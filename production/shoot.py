"""One call shoots a scene.

Owner 2026-09-24: after the writer has written the shot cards, "拍这几颗" is the last manual step. The order is taken
at once (method, prepare with the deterministic gates, four takes per card through the ordinary batch path); the
worker's AutoShoot then waits for the takes and puts the shot on the desk. It never picks, never retries a failed
take (Higgsfield regenerates by a human decision with one line changed, `briefs-clean/hell-grind.txt:116`), never
runs an AI review and never listens to the takes (the owner watches them).
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from production.auth import AuthService, Principal
from production.contracts import (
    BatchRequest,
    Contract,
    DecisionRequest,
    DomainError,
    Identifier,
    MethodSelection,
    Mutation,
    ObjectRef,
    PrepareRequest,
    content_hash,
)
from production import switches
from production.store import Store

KIND, AUTHOR = 'shoot-order', 'shoot_service'
# the released video method (fal 480p drafts, used when 样片模式 is on) and the Higgsfield
# 1080p method every other order uses.
DEFAULT_VIDEO_METHOD, HF_VIDEO_METHOD = 'mvgp-video-v1', 'mvgp-video-hf-v1'
# The asset roles that carry an image a shot can reference (voice and behaviour are text for the writer).
IMAGE_ROLES = ('visual', 'world', 'state', 'delivery', 'look')
MAKING = ('queued', 'dispatching', 'submitted', 'running')
# An unknown take usually settles by itself from the Higgsfield listing within minutes;
# if it still has not after this long, the settled takes go to the desk without it.
UNKNOWN_PATIENCE_SECONDS = 900
# 'listening' is only in orders written by the unreleased release-80 build; such an order goes straight to the desk.
OPEN = ('firing', 'listening')
# The desk's 再拍一批 without a sentence (production/web/review.js decline(): "再拍一批：（没写原因）").
REASONLESS_REBATCH = re.compile(r'^\s*再拍一批[：:]\s*(（没写原因）)?\s*$')


# the desk shows the owner a plain-Chinese reason; the English reason stays for the agent.
OWNER_REASONS = {
    'budget_exceeded': '这个项目的预算用完了，要加预算才能接着拍',
    'card_changed': '卡片下单后又改过了，要按新卡重新下单',
    'takes_failed': '这一批一条都没出来，要改一句再拍',
    'no_takes': '这一批的结果还没回来（结果不明），先别重拍',
    'project_changing': '下单时项目一直在变，没下成，再下一次',
    'stale_input': '卡片或素材变了，要重新准备这张卡',
    'missing_prerequisite': '这张卡还缺东西，准备不了',
    'unsupported_route': '这张卡的设置平台不支持',
    'unsupported_method': '这张卡的设置平台不支持',
    'rule_violation': '这张卡没过平台的检查',
    'invalid_input': '这张卡写得不对，平台读不懂',
    'locked': '这一段已经定稿锁住了，要先解锁',
    'unexpected': '出了意外错误，要看一下日志',
}


def owner_reason(entry: dict[str, Any]) -> str:
    """The owner's one line for a stopped card (old records without a code get the generic line)."""
    return OWNER_REASONS.get(str(entry.get('code') or ''), '这张卡停下了，Agent 会看原因再处理')


Line = Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class ReviewNote(Contract):
    """One finding of the fresh reviewer and the writer's answer (Q6, cully:25)."""
    line: Line          # the manual line the finding names, e.g. cinedance:336; a finding without one is not a finding
    note: Line          # what the reviewer found
    answer: Line        # the writer's one line: which section it patched
    shot: Annotated[str, StringConstraints(max_length=64)] | None = None


def _section(config: Any, role: str) -> dict[str, Any]:
    try:
        found = config.section(role)
    except DomainError:
        return {}
    return found if isinstance(found, dict) else {}


class ShootReview(Contract):
    reviewer: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    playbook_version: Annotated[str, StringConstraints(max_length=64)] | None = None
    notes: Annotated[list[ReviewNote], Field(max_length=400)] = Field(default_factory=list)


class ShootRequest(Mutation):
    cards: Annotated[list[ObjectRef], Field(min_length=1, max_length=40)]
    takes: Annotated[int, Field(ge=1, le=4)] = 4
    # The released video method; Shoot.video_method maps it to Higgsfield, or to fal when 样片模式 is on.
    method_id: Identifier = 'mvgp-video-v1'
    task: Literal['shot', 'stress'] = 'shot'
    # (Q6): the reviewer's notes and the writer's answers travel with the order and show on
    # the desk; the platform never blocks on them ("没审" when absent).
    review: ShootReview | None = None

    def stored(self) -> dict[str, Any]:
        """The request as the order records it; without a review it is byte-for-byte the shape orders had before reviewer notes were added,
        so replaying an older order's key still finds its order."""
        dumped = self.model_dump(mode='json')
        if dumped.get('review') is None:
            dumped.pop('review', None)
        return dumped


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: obj[k] for k in ('object_id', 'revision', 'digest')}


class Shoot:
    """The agent's one call. Everything it does goes through the ordinary services and their own checks."""

    def __init__(self, store: Store, auth: AuthService, projects: Any, compiler: Any, batches: Any) -> None:
        self.store, self.auth, self.projects, self.compiler, self.batches = store, auth, projects, compiler, batches

    def _inputs(self, pid: str, card: dict[str, Any], db: sqlite3.Connection) -> list[ObjectRef]:
        """The assets this card shoots with: the prompt's @tags are the references, as HF
        has no binding object ("the same tags are used everywhere: in documents, in prompts, in the interface",
        hell-grind:59). Current selections made for the card or its scene are kept; every prompt tag (and the card's
        named look) that none covers gets a selection of the one current asset carrying that tag; with one look in
        the project and none named, that look. A selection made for an older scene is left out, never an error."""
        from production.prompt import tags as prompt_tags
        content: dict[str, Any] = card['body']['content'] if isinstance(card['body'].get('content'), dict) else {}
        production: dict[str, Any] = content['_production'] if isinstance(content.get('_production'), dict) else {}
        scenes = {d['object_id'] for d in card['body'].get('dependencies', [])
                  if self.store.get_object(pid, d['object_id'], conn=db)['kind'] == 'scene'}
        chosen: list[ObjectRef] = []
        covered: set[str] = set()
        assets = self.store.list_objects(pid, kind='asset', conn=db)
        for asset in assets:
            selection = asset['body'].get('content') if isinstance(asset['body'].get('content'), dict) else {}
            target = selection.get('target') or {}
            if selection.get('type') != 'asset-selection' or target.get('object_id') not in {*scenes, card['object_id']}:
                continue
            if target.get('revision') != self.store.get_object(pid, target['object_id'], conn=db)['revision']:
                continue  # made for an older version of the scene or card
            picked = [r for r in (selection.get('selected') or {}).values() if isinstance(r, dict)]
            if any(r.get('revision') != self.store.get_object(pid, r['object_id'], conn=db)['revision'] for r in picked):
                continue  # names an older version of an asset (a new picture or descriptor): the tags pick the current one
            chosen.append(ObjectRef(**_ref(asset)))
            for ref in (selection.get('selected') or {}).values():
                tag = (self.store.get_object(pid, ref['object_id'], revision=ref['revision'], conn=db)['body'].get('content') or {}).get('tag')
                if isinstance(tag, str):
                    covered.add(tag.lstrip('@'))
        elements = [a for a in assets if isinstance(a['body'].get('content'), dict) and a['body']['content'].get('type', 'asset') == 'asset'
                    and a['body']['content'].get('role') in IMAGE_ROLES]
        wanted = prompt_tags(str(production.get('prompt') or ''))
        look = str(production.get('look') or '').lstrip('@')
        looks = [a for a in elements if a['body']['content']['role'] == 'look']
        if look:
            wanted.append(look)
        elif len(looks) == 1 and not any(t in covered for t in (str(a['body']['content'].get('tag', '')).lstrip('@') for a in looks)):
            wanted.append(str(looks[0]['body']['content']['tag']).lstrip('@'))
        for tag in dict.fromkeys(wanted):
            if tag in covered:
                continue
            owners = [a for a in elements if a['body']['content'].get('tag') == '@' + tag]
            if len(owners) != 1:
                continue  # none, or two assets under one tag: the compiler's advice names the tag
            asset = owners[0]
            role = asset['body']['content']['role']
            oid = 'sel_' + content_hash({'card': _ref(card), 'asset': _ref(asset)})[:32]
            try:
                derived = self.store.get_object(pid, oid, conn=db)
            except DomainError:
                derived = self.store.create_object(pid, 'asset', {
                    'content': {'type': 'asset-selection', 'target': _ref(card), 'selected': {role: _ref(asset)}},
                    'dependencies': [_ref(asset)], 'logical_path': f'selections/{tag}.json'}, AUTHOR, object_id=oid, conn=db)
            chosen.append(ObjectRef(**_ref(derived)))
            covered.add(tag)
        return chosen

    def _method(self, actor: Principal, pid: str, card: dict[str, Any], method_id: str, key: str) -> ObjectRef:
        with self.store.transaction(write=False) as db:
            old = next((m for m in self.store.list_objects(pid, kind='method', conn=db)
                        if (m['body'].get('content') or {}).get('target', {}).get('object_id') == card['object_id']), None)
        content = (old or {}).get('body', {}).get('content') or {}
        if old is not None and content.get('method_id') == method_id and content.get('target') == _ref(card):
            return ObjectRef(**_ref(old))  # already chosen for this card version
        chosen = self.projects.select_method(actor, pid, MethodSelection(
            idempotency_key=key, expected_revision=old['revision'] if old else card['revision'], target=ObjectRef(**_ref(card)),
            method_id=method_id, rationale='Shoot order: the released video method for this card.'))
        return ObjectRef(**chosen['object_ref']) if 'object_ref' in chosen else ObjectRef(**_ref(chosen))

    def fire(self, actor: Principal, pid: str, candidate_id: str, takes: int, key: str) -> dict[str, Any]:
        """Four takes through the batch path."""
        for _ in range(2):
            project = self.store.get_object(pid, pid)
            try:
                batch = self.batches.create(actor, pid, BatchRequest(idempotency_key=key, expected_revision=project['revision'],
                                                                     candidate_ids=[candidate_id] * takes))
                return {'stage': 'firing', 'batch': _ref(batch) if 'object_id' in batch else batch['object_ref']}
            except DomainError as exc:
                if exc.code == 'revision_conflict':
                    continue
                return {'stage': 'stopped', 'reason': exc.message, 'code': exc.code}
        return {'stage': 'stopped', 'reason': 'The project kept changing while firing; order again', 'code': 'project_changing'}

    def quote(self, actor: Principal, pid: str, cards: list[ObjectRef], takes: int = 4) -> dict[str, Any]:
        """What shooting these cards would cost, read-only (audit: "quote spend before shoot").
        Per card: the card's seconds × the route's price per second × the takes, plus the 1080p completion of the one
        take the owner picks; the envelope left after what is spent and held; advice when it does not cover the order.
        Prices come from runtime.json (the same numbers the worker settles with)."""
        config = self.batches.workflow.config
        cost = config.section('execution_policy')['operations']
        # compiler → context → queries → shoot, so the import happens at call time.
        from production.compiler import route_defaults
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'context', conn=db)
            # What the compiler fills in when a card leaves it out, for the method an order would use now.
            defaults = route_defaults(config, self.video_method(pid, DEFAULT_VIDEO_METHOD, db))
            rows: list[dict[str, Any]] = []
            held, budget_key = 0, None
            for ref in cards:
                card = self.store.get_object(pid, ref.object_id, conn=db)
                content: dict[str, Any] = card['body'].get('content') if isinstance(card['body'].get('content'), dict) else {}
                production: dict[str, Any] = content['_production'] if isinstance(content.get('_production'), dict) else {}
                model = str(production.get('model') or defaults.get('model') or '')
                resolution = str(production.get('resolution') or defaults.get('resolution') or '')
                material: dict[str, Any] = content['The material'] if isinstance(content.get('The material'), dict) else {}
                seconds = material.get('the running time in seconds')
                policy = cost.get('submit', {}).get(model)
                if policy is None or type(seconds) is not int:
                    rows.append({'card': _ref(card), 'shot': content.get('shot'), 'advice': '这张卡的路线或秒数平台算不出价'})
                    continue
                capability: dict[str, Any] = config.section('fal_video_capability') if model.startswith('fal_') else {}
                rate = (capability.get('usd_micros_per_second') or {}).get(resolution) if capability.get('job_type') == model else None
                if not model.startswith('fal_') and policy.get('budget_unit') == 'hf_credit':
                    # Higgsfield credits per second, the price its takes settle at.
                    price = _section(config, 'hf_video_price')
                    rate = (price.get('credits_per_second') or {}).get(resolution) if price.get('job_type') == model else None
                completion = cost.get('complete-draft', {}).get(f'{model}_complete')
                full_rate = ((config.section('fal_complete_capability').get('usd_micros_per_second') or {}).get('1080p')
                             if completion else None)
                each = rate * seconds if isinstance(rate, int) else policy['estimated_cost']
                rows.append({'card': _ref(card), 'shot': content.get('shot'), 'seconds': seconds, 'takes': takes,
                             'unit': policy['budget_unit'], 'per_take': each, 'drafts': each * takes,
                             'completion': full_rate * seconds if isinstance(full_rate, int) else None,
                             'held': policy['reservation'] * takes})
                held += policy['reservation'] * takes
                budget_key = policy.get('budget_key', 'legacy')
            envelope = None
            if budget_key is not None:
                try:
                    budget = self.store.budget(pid, budget_key=budget_key, conn=db)
                    envelope = {'budget_key': budget_key, 'unit': budget['unit'],
                                'left': budget['ceiling'] - budget['spent'] - budget['reserved']}
                except DomainError:
                    envelope = {'budget_key': budget_key, 'unit': None, 'left': 0}
        total = sum(int(r.get('drafts') or 0) + int(r.get('completion') or 0) for r in rows)
        advice = [r['advice'] for r in rows if 'advice' in r]
        if envelope is not None and held > envelope['left']:
            advice.append(f"预算不够这一单：要先占 {held} {envelope['unit']}，只剩 {envelope['left']}")
        return {'cards': rows, 'total': total, 'held': held, 'envelope': envelope, 'advice': advice}

    def video_method(self, pid: str, method_id: str, conn: Any = None) -> str:
        """(owner 2026-09-27): the released video method is Higgsfield at 1080p; a film with
        样片模式 on shoots fal's 480p drafts instead. A take never moves between the two, so the switch decides at
        order time and each order records the method it used."""
        if method_id != DEFAULT_VIDEO_METHOD:
            return method_id
        config = self.batches.workflow.config
        if switches.current(self.store, config, pid, conn).get('sample_mode') is True:
            return DEFAULT_VIDEO_METHOD
        try:
            config.require_method(HF_VIDEO_METHOD, task='shot')
        except DomainError:
            return DEFAULT_VIDEO_METHOD  # a config without the Higgsfield method (the test world) keeps its own
        return HF_VIDEO_METHOD

    def order(self, actor: Principal, pid: str, request: ShootRequest) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'prepare', conn=db)
            method_id = self.video_method(pid, request.method_id, db)
        order_id = 'shoot_' + content_hash({'pid': pid, 'actor': actor.credential_id, 'key': request.idempotency_key})[:32]
        try:
            existing = self.store.get_object(pid, order_id)
            if existing['body'].get('request') != request.stored():
                raise DomainError('idempotency_conflict', 'This shoot order key was used with other cards')
            return self.view(existing)
        except DomainError as exc:
            if exc.code != 'not_found':
                raise
        body = {'request': request.stored(), 'method_id': method_id,
                'origin': {'actor_id': actor.actor_id, 'credential_id': actor.credential_id},
                **({'review': request.review.model_dump(mode='json')} if request.review is not None else {}),
                'cards': [{'card': ref.model_dump(), 'takes': request.takes, 'stage': 'ordering'} for ref in request.cards],
                'dependencies': [ref.model_dump() for ref in request.cards]}
        # Recorded before anything is paid, so every fired batch has an order that finishes it.
        order = self.store.create_object(pid, KIND, body, AUTHOR, object_id=order_id)
        for n, ref in enumerate(request.cards):
            key = f'{order_id}-{n}'
            entry: dict[str, Any] = {}
            try:
                with self.store.transaction() as db:  # derived selections are written here
                    card = self.store.get_object(pid, ref.object_id, conn=db)
                    if card['kind'] != 'shot' or _ref(card) != ref.model_dump():
                        raise DomainError('stale_input', 'Name the current version of a shot card')
                    entry['shot'] = (card['body'].get('content') or {}).get('shot')
                    # (owner 2026-09-26): an unchanged card may be shot again, as HF re-fires an
                    # identical prompt in 42–52% of batches (trigger, red-flag, oneiric). A repeated click is the same
                    # idempotency key, which returns the order above and pays nothing twice.
                    inputs = self._inputs(pid, card, db)
                method = self._method(actor, pid, card, method_id, key + '-method')
                candidate = self.compiler.prepare(actor, pid, PrepareRequest(
                    idempotency_key=key + '-prepare', expected_revision=card['revision'], target=ref, task=request.task,
                    method_selection=method, inputs=inputs))
                entry['candidate'] = candidate['object_ref'] if 'object_ref' in candidate else _ref(candidate)
                entry.update(self.fire(actor, pid, entry['candidate']['object_id'], request.takes, key + '-batch'))
            except DomainError as exc:
                entry.update(stage='stopped', reason=exc.message, code=exc.code)
            except Exception as exc:  # noqa: BLE001 -- a surprise on one card must not orphan another card's paid batch.
                entry.update(stage='stopped', reason=f'Unexpected {type(exc).__name__} while ordering this card', code='unexpected')
            order = self._update(pid, order_id, n, entry)
        return self.view(order)

    def _update(self, pid: str, order_id: str, n: int, changes: dict[str, Any]) -> dict[str, Any]:
        with self.store.transaction() as db:
            fresh = self.store.get_object(pid, order_id, conn=db)
            cards = [dict(c) for c in fresh['body']['cards']]
            cards[n].update(changes)
            return self.store.append_revision(pid, order_id, fresh['revision'], {**fresh['body'], 'cards': cards}, AUTHOR, conn=db)

    @staticmethod
    def view(order: dict[str, Any]) -> dict[str, Any]:
        return {'order': _ref(order), 'cards': [{**{k: c.get(k) for k in ('shot', 'stage', 'reason', 'card', 'candidate', 'batch', 'request')},
                                                 'owner_reason': owner_reason(c) if c.get('stage') == 'stopped' else None}
                                                for c in order['body']['cards']]}


class AutoShoot:
    """Advances open shoot orders one step per call: wait for the takes, then offer them on the desk."""

    def __init__(self, principal: Principal, store: Store, shoot: Shoot, decisions: Any, media: Any, *,
                 reshoot_after_event: int | None = None) -> None:
        if principal.role != 'agent':
            raise ValueError('The shoot service needs its own agent credential')
        if reshoot_after_event is not None and (type(reshoot_after_event) is not int or reshoot_after_event < 0):
            raise ValueError('reshoot_after_event must be an event number')
        self.principal, self.store, self.shoot, self.decisions, self.media = principal, store, shoot, decisions, media
        # only 再拍一批 answers recorded after this event (the deploy's cutoff) fire by
        # themselves; None (not configured) means no automatic reshoot at all.
        self.reshoot_after_event = reshoot_after_event

    def _save(self, pid: str, order: dict[str, Any], n: int, changes: dict[str, Any]) -> None:
        self.shoot._update(pid, order['object_id'], n, changes)

    def advance(self, pid: str) -> str:
        """One step for every open card, so a card waiting on Higgsfield never holds up another one."""
        with self.store.transaction(write=False) as db:
            orders = [o for o in self.store.list_objects(pid, kind=KIND, conn=db) if o['author'] == AUTHOR]
        states = self._reshoots(pid)
        for order in orders:
            for n, entry in enumerate(order['body']['cards']):
                if entry.get('stage') not in OPEN:
                    continue
                try:
                    states.append(self._step(pid, order, n, entry))
                except DomainError as exc:
                    states.append(f"{entry.get('shot') or n}: blocked:{exc.code}")
        return '; '.join(states) or 'idle'

    def _reshoots(self, pid: str) -> list[str]:
        """(owner 2026-09-26): 再拍一批 without a sentence re-fires the same card, four takes of
        the same candidate, once per answer (HF re-fires identical prompts in 42–52% of batches). An answer with a
        sentence is left for the agent to patch the card; a changed card, an undone answer or an answer from before
        the cutoff never fires. The budget and the in-flight guard still apply (Batches.create)."""
        if self.reshoot_after_event is None:
            return []
        states = []
        with self.store.transaction(write=False) as db:
            receipts = [r for r in self.store.list_objects(pid, kind='human-receipt', conn=db)
                        if r['author'] == 'decision_service' and r['body'].get('purpose') == 'take'
                        and r['body'].get('choice') == 'decline' and REASONLESS_REBATCH.match(str(r['body'].get('reason') or '再拍一批：'))]
            made = dict(db.execute(
                "SELECT json_extract(body,'$.object_id'), sequence FROM events WHERE project_id=? AND kind='object.created' "
                "AND json_extract(body,'$.object_id') IN (" + ','.join('?' for _ in receipts) + ')',
                [pid, *(r['object_id'] for r in receipts)]).fetchall()) if receipts else {}
            orders = [o for o in self.store.list_objects(pid, kind=KIND, conn=db) if o['author'] == AUTHOR]
        for receipt in receipts:
            order_id = 'shoot_' + content_hash({'pid': pid, 'reshoot': receipt['object_id']})[:32]
            existing = next((o for o in orders if o['object_id'] == order_id), None)
            if existing is not None:  # an order made before a deploy still finishes (the cutoff is for new ones)
                entry = existing['body']['cards'][0]
                if entry.get('stage') == 'waiting':
                    states.append(self._refire(pid, existing, entry))
                continue
            if made.get(receipt['object_id'], 0) <= self.reshoot_after_event:
                continue
            decision = (receipt['body'].get('decision') or {}).get('object_id')
            if not isinstance(decision, str):
                continue
            source = next(((o, c) for o in orders for c in o['body'].get('cards', [])
                           if (c.get('request') or {}).get('object_id') == decision and c.get('candidate')), None)
            if source is None:
                continue
            order, entry = source
            card = self.store.get_object(pid, entry['card']['object_id'])
            request = self.store.get_object(pid, decision)
            if _ref(card) != entry['card'] or not self._still_answered(request, receipt):
                continue  # the card changed (the agent acted) or the answer was undone
            body = {'request': {'reshoot_of': _ref(receipt)}, 'origin': {'actor_id': self.principal.actor_id,
                    'credential_id': self.principal.credential_id},
                    'cards': [{'card': entry['card'], 'takes': entry.get('takes', 4), 'stage': 'waiting', 'shot': entry.get('shot'),
                               'candidate': entry['candidate'], 'reshoot_of': _ref(receipt),
                               **({'review': entry['review']} if 'review' in entry else {})}],
                    'dependencies': [entry['card'], _ref(receipt)]}
            if 'review' in order['body']:
                body['review'] = order['body']['review']  # the reviewer's notes and answers stay with the card
            try:
                created = self.store.create_object(pid, KIND, body, AUTHOR, object_id=order_id)
            except DomainError as exc:
                if exc.code != 'revision_conflict':
                    raise
                continue  # another worker recorded it first
            states.append(self._refire(pid, created, created['body']['cards'][0]))
        return states

    @staticmethod
    def _still_answered(request: dict[str, Any], receipt: dict[str, Any]) -> bool:
        """The owner's answer is still the latest word on the request: the request is at the very revision this answer
        made. An undo moves it on, and a new 再拍一批 after the undo is a new answer with its own receipt; so one
        answer never fires twice and an undone one never fires (bug hunt 2026-09-27)."""
        answered = receipt['body'].get('decision') or {}
        return request['body'].get('state') == 'declined' and request['revision'] == answered.get('revision')

    def _refire(self, pid: str, order: dict[str, Any], entry: dict[str, Any]) -> str:
        label = entry.get('shot') or entry['card']['object_id']
        card = self.store.get_object(pid, entry['card']['object_id'])
        if _ref(card) != entry['card']:
            self._save(pid, order, 0, {'stage': 'stopped', 'reason': 'The card changed before the reshoot fired', 'code': 'card_changed'})
            return f'{label}: stopped (card changed)'
        receipt = self.store.get_object(pid, entry['reshoot_of']['object_id'])
        request = self.store.get_object(pid, receipt['body']['decision']['object_id'])
        if not self._still_answered(request, receipt):
            self._save(pid, order, 0, {'stage': 'stopped', 'reason': 'The owner undid this 再拍一批 before it fired', 'code': 'undone'})
            return f'{label}: stopped (undone)'
        fired = self.shoot.fire(self.principal, pid, entry['candidate']['object_id'], entry.get('takes', 4),
                                'reshoot-' + entry['reshoot_of']['object_id'])
        if fired.get('code') in ('unknown_outcome', 'project_changing'):
            # An earlier take of the same request is still out, or the project kept moving: try again next cycle.
            return f'{label}: waiting'
        self._save(pid, order, 0, fired)
        return f"{label}: {fired['stage']}"

    def _step(self, pid: str, order: dict[str, Any], n: int, entry: dict[str, Any]) -> str:
        label = entry.get('shot') or entry['card']['object_id']
        card = self.store.get_object(pid, entry['card']['object_id'])
        if _ref(card) != entry['card']:
            self._save(pid, order, n, {'stage': 'stopped', 'reason': 'The card changed after the order; order it again', 'code': 'card_changed'})
            return f'{label}: stopped (card changed)'
        if entry['stage'] == 'firing':
            batch = self.store.get_object(pid, entry['batch']['object_id'])
            jobs = [self.store.get_object(pid, c['job']['object_id']) for c in batch['body']['children']]
            if any(j['body'].get('state') in MAKING for j in jobs):
                return f'{label}: firing'
            unknown = [n2 + 1 for n2, j in enumerate(jobs) if j['body'].get('state') == 'unknown']
            if unknown:
                since = entry.get('unknown_since')
                if since is None:
                    self._save(pid, order, n, {'unknown_since': time.time()})
                    return f'{label}: firing'
                if time.time() - since < UNKNOWN_PATIENCE_SECONDS:
                    return f'{label}: firing'
            takes = [j['body']['result'] for j in jobs if j['body'].get('state') == 'succeeded' and j['body'].get('result')]
            if not takes:
                self._save(pid, order, n, {'stage': 'stopped', 'reason': 'Every take failed at the provider; rewrite one line and order again'
                                           if not unknown else f'No take came back (take {", ".join(map(str, unknown))} still unknown)',
                                           'code': 'no_takes' if unknown else 'takes_failed'})
                return f'{label}: stopped (no take)'
            notes = [f'第 {", ".join(map(str, unknown))} 条结果不明，没送上来'] if unknown else []
        else:
            takes, notes = entry['offered_takes'], list(entry.get('extra_notes') or [])
        request = self.decisions.request(self.principal, pid, DecisionRequest(
            idempotency_key=f"{order['object_id']}-{n}-offer", expected_revision=card['revision'], target=ObjectRef(**_ref(card)),
            shot=ObjectRef(**_ref(card)), takes=[ObjectRef(**t) for t in takes], purpose='take',
            rationale='；'.join(notes) or f'{len(takes)} 条拍好了'))
        self._save(pid, order, n, {'stage': 'on-desk', 'request': request['object_ref'] if 'object_ref' in request else _ref(request)})
        return f'{label}: on-desk'
