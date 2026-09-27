"""One-use human decisions bound to exact takes, cuts or budget envelopes."""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any

from production.auth import AuthService, Principal
from production.contracts import (
    DecisionRequest,
    DomainError,
    HumanDecision,
    ObjectRef,
)
from production.cuts import Cuts
from production.store import Store
from production.workflow import Workflow

ReceiptCheck = Callable[..., list[dict[str, Any]]]


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k:obj[k] for k in ('object_id','revision','digest')}


class Decisions:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow, cuts: Cuts, *,
                 receipt_check: ReceiptCheck | None = None, human_confirmation_enabled: bool = False,
                 clock: Callable[[], float] = time.time, ttl_seconds: int = 1800) -> None:
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 86400:
            raise ValueError('Decision expiry must be bounded')
        self.store,self.auth,self.workflow,self.cuts = store,auth,workflow,cuts
        self.receipt_check,self.human_enabled = receipt_check,human_confirmation_enabled
        self.clock,self.ttl = clock,ttl_seconds

    def _target(self, pid: str, ref: ObjectRef, db: sqlite3.Connection, *, current: bool = True) -> dict[str, Any]:
        obj = self.store.get_object(pid,ref.object_id,revision=ref.revision,conn=db)
        if ref.digest is None or obj['digest'] != ref.digest:
            raise DomainError('stale_input','Decision target fingerprint differs')
        if current and self.workflow.pinned_graph(pid,ref,conn=db)['stale']:
            raise DomainError('stale_input','Decision target is no longer current')
        return obj

    def _final(self, pid: str, ref: ObjectRef, db: sqlite3.Connection) -> dict[str, Any]:
        target = self._target(pid,ref,db)
        if target['kind'] != 'media' or not target['body'].get('source_cut') or not target['body']['probe'].get('has_video'):
            raise DomainError('invalid_input','Final confirmation requires actual finished cut video')
        cut_ref = ObjectRef.model_validate(target['body']['source_cut'])
        cut = self._target(pid,cut_ref,db)
        if cut['author'] != 'cut_service' or cut['kind'] != 'cut':
            raise DomainError('forbidden','Final output needs a service-issued edit manifest')
        graph = self.workflow.pinned_graph(pid,ref,conn=db)
        if _ref(cut) not in [n['ref'] for n in graph['nodes']]:
            raise DomainError('stale_input','Finished video lacks its source-cut dependency')
        invalidations = self.store.list_objects(pid,kind='invalidation',conn=db)
        ids = {n['ref']['object_id'] for n in graph['nodes']}
        if any(i['body'].get('target',{}).get('object_id') in ids for i in invalidations):
            raise DomainError('stale_input','Reopened finishing must be redone for a new cut')
        # no finishing records and no AI cut review (HF has neither). A final is the cut and
        # its rendered video; older finals may still carry 'receipts'/'finishing' evidence, which stays readable.
        return {'cut':_ref(cut),'media':_ref(target)}

    def _take_lineage(self, pid: str, shot_ref: ObjectRef, take_ref: ObjectRef,
                      db: sqlite3.Connection, *, current: bool = True) -> dict[str, Any]:
        # A pick names the card version its takes were made for: after 再拍一批 the card
        # moves on, and the owner may still pick a take of the earlier batch.
        shot = self._target(pid, shot_ref, db, current=current)
        take = self._target(pid, take_ref, db)
        if shot['kind'] != 'shot' or take['kind'] != 'media' or not take['body'].get('probe', {}).get('has_video'):
            raise DomainError('invalid_input', 'Select a real video take for a shot')
        self.workflow.guard_mutation(self.store, pid, shot['object_id'], db)
        graph = self.workflow.pinned_graph(pid, take_ref, conn=db)
        if _ref(shot) not in [node['ref'] for node in graph['nodes']]:
            raise DomainError('stale_input', 'Take must derive from the current shot')
        origin = self.workflow.media_origin(pid, take_ref, conn=db)
        if (take['author'] != 'worker_service' or origin['kind'] != 'candidate'
                or origin['authority']['body']['target'] != _ref(shot)):
            raise DomainError('invalid_input', 'Selection needs exact service generation lineage')
        return dict(origin['authority']['body'])

    def _takes(self, pid: str, shot: ObjectRef, takes: list[ObjectRef], db: sqlite3.Connection) -> dict[str, Any]:
        for take in takes:
            self._take_lineage(pid, shot, take, db)
        return {'shot': shot.model_dump(), 'takes': [take.model_dump() for take in takes]}

    def request(self, actor: Principal, pid: str, request: DecisionRequest) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor,pid,'request-decision',conn=db)
            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                target = self._target(pid,request.target,conn)
                if request.expected_revision != target['revision']:
                    raise DomainError('revision_conflict','Decision target revision differs')
                if request.purpose == 'final':
                    evidence = self._final(pid,request.target,conn)
                elif request.purpose == 'take':
                    if request.shot is None or request.takes is None or request.shot != request.target:
                        raise DomainError('invalid_input', 'Take decision must target its shot and offer takes')
                    evidence = self._takes(pid, request.shot, request.takes, conn)
                elif request.purpose == 'shot-plan':
                    # Higgsfield has no shot-plan approval.
                    # The purpose stays in the contract so release-79 records remain readable.
                    raise DomainError('invalid_input', 'There is no shot-plan step; shoot the cards and offer the takes')
                else:
                    budget_key = request.budget_key or 'legacy'
                    budget = self.store.budget(pid,budget_key=budget_key,conn=conn)
                    if request.budget_unit != budget['unit'] or request.proposed_limit is None or request.proposed_limit < budget['spent']+budget['reserved']:
                        raise DomainError('invalid_input','Envelope must preserve actual spend/reservations and its budget unit')
                    budget_revision = conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE project_id=? "
                        "AND kind IN ('budget.changed','operator.envelope.changed') "
                        "AND COALESCE(json_extract(body,'$.budget_key'),'legacy')=?", (pid,budget_key)).fetchone()[0]
                    evidence = {'budget_before':budget,'proposed_limit':request.proposed_limit,'budget_unit':request.budget_unit,
                                'budget_key':budget_key,'budget_revision':budget_revision}
                for old in self.store.list_objects(pid,kind='decision-request',conn=conn):
                    if (old['body']['purpose'] == request.purpose and old['body']['state'] == 'pending'
                            and (request.purpose == 'final'
                                 or (request.purpose in ('take', 'shot-plan')
                                     and old['body']['target']['object_id'] == target['object_id'])
                                 or (request.purpose == 'envelope'
                                     and old['body']['evidence'].get('budget_key','legacy') == evidence['budget_key']))):
                        self.store.append_revision(pid,old['object_id'],old['revision'],{**old['body'],'state':'superseded'},'decision_service',conn=conn)
                return self.store.create_object(pid,'decision-request',{
                    'target':_ref(target),'target_hash':target['digest'],'purpose':request.purpose,'rationale':request.rationale,
                    'evidence':evidence,'state':'pending','expires_at':self.clock()+self.ttl,
                    'human_confirmation_available':self.human_enabled,'requested_by':actor.actor_id,
                    'dependencies':[_ref(target)]},'decision_service',conn=conn)
            return self.store.run_idempotent(f'{pid}:{actor.credential_id}:request-decision',request.idempotency_key,
                                             request.model_dump(),save,conn=db)

    def _pick_without(self, pid: str, shot_id: str, request_id: str, conn: sqlite3.Connection) -> dict[str, Any] | None:
        """The shot's pick with this decision request left out: every other request keeps its own latest pick (a
        withdrawal empties it), and the shot takes the pick of the most recently confirmed request that still has one."""
        from production.queries import creation_order
        receipts = {r['object_id']: ((r['body'].get('decision') or {}).get('object_id'), r['body'].get('choice'))
                    for r in self.store.list_objects(pid, kind='human-receipt', conn=conn)}
        rows = [r for r in self.store.list_objects(pid, kind='human-take-selection', conn=conn)
                if r['author'] == 'decision_service' and (r['body'].get('shot') or {}).get('object_id') == shot_id]
        order = creation_order(conn, pid, [r['object_id'] for r in rows])
        picks: dict[str, dict[str, Any] | None] = {}
        for r in sorted(rows, key=lambda r: order[r['object_id']]):
            owner, choice = receipts.get((r['body'].get('human_receipt') or {}).get('object_id'), (None, None))
            if owner is None or owner == request_id:
                continue
            picks.pop(owner, None)  # re-inserted last: the most recent decision of that request
            picks[owner] = r['body'].get('take') if choice == 'confirm' else None
        return next((take for take in reversed(list(picks.values())) if take), None)

    def decide(self, human: Principal, pid: str, request: HumanDecision, *, origin: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(human,pid,'human-decision',origin=origin,csrf_token=request.csrf_token,conn=db)
            if not self.human_enabled:
                raise DomainError('forbidden','Independent human-session deployment premise is not enabled')
            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                pending = self.store.get_object(pid,request.request_id,conn=conn)
                body = pending['body']
                # a take request never expires. The owner may pick any offered take of any
                # batch (answered, pending or superseded by a newer batch) at the card version it was made for, or
                # withdraw a pick; only 再拍一批 / 都不行 needs the current pending batch. The latest record wins.
                take = body['purpose'] == 'take'
                if (pending['kind'] != 'decision-request' or pending['author'] != 'decision_service'
                        or body['target_hash'] != request.target_hash
                        or not take and not (body['state'] == 'pending' and body['expires_at'] > self.clock())):
                    raise DomainError('stale_input','Decision expired, was superseded, or names another version')
                withdrawing = take and request.choice == 'decline' and body['state'] == 'confirmed'
                declining = take and request.choice == 'decline' and not withdrawing
                if declining and body['state'] != 'pending':
                    raise DomainError('stale_input','No pick is recorded for this shot, and this batch is already answered')
                # 撤销 takes back a 再拍一批 / 都不行 answer while the card is unchanged; once the
                # agent has patched the card (acted on the sentence), the owner picks from the batch instead.
                undoing = request.choice == 'undo'
                if undoing and not (take and body['state'] == 'declined'):
                    raise DomainError('invalid_input','Only a 再拍一批 / 都不行 answer can be undone')
                if undoing and request.selected_take is not None:
                    raise DomainError('invalid_input','Undo names no take')
                target = ObjectRef.model_validate(body['target'])
                self._target(pid,target,conn,current=not take or declining or undoing)
                if take and (withdrawing or request.choice == 'confirm'):
                    # after 这版可以 the owner may still change a pick; that reopens this shot
                    # and the cut, and the film is cut again (the desk promises it, review.js "平台会再拼一版").
                    self.workflow.reopen_for_owner(pid, target.object_id,
                                                   request.reason or 'The owner changed a pick after confirming the film', conn=conn)
                if withdrawing:
                    self.workflow.guard_mutation(self.store, pid, target.object_id, conn)
                selecting = body['purpose'] == 'take' and request.choice == 'confirm'
                if (request.selected_take is not None) != selecting:
                    raise DomainError('invalid_input', 'Select a take exactly when confirming a take decision')
                if request.choice == 'confirm':
                    if body['purpose'] == 'final':
                        evidence = self._final(pid,target,conn)
                        # Only the picture must match (an older request may also carry finishing records).
                        if any(evidence[key] != body['evidence'].get(key) for key in ('cut','media')):
                            raise DomainError('stale_input','The cut changed; request a fresh decision')
                    elif body['purpose'] == 'take':
                        if request.selected_take is None or request.selected_take.model_dump() not in body['evidence']['takes']:
                            raise DomainError('invalid_input', 'Selected take is not an exact offered reference')
                        self._take_lineage(pid, target, request.selected_take, conn, current=False)
                    elif body['purpose'] == 'shot-plan':
                        pass  # the scene target was re-checked above; each shot's card is checked again at submit
                    else:
                        evidence = body['evidence']
                        budget_key = evidence.get('budget_key','legacy')
                        budget = self.store.budget(pid,budget_key=budget_key,conn=conn)
                        budget_revision = conn.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE project_id=? "
                            "AND kind IN ('budget.changed','operator.envelope.changed') "
                            "AND COALESCE(json_extract(body,'$.budget_key'),'legacy')=?", (pid,budget_key)).fetchone()[0]
                        if (budget['ceiling'] != evidence['budget_before']['ceiling'] or budget['unit'] != evidence['budget_unit']
                                or evidence.get('budget_revision') != budget_revision):
                            raise DomainError('stale_input','Envelope changed since the request')
                        self.store.set_budget(pid,evidence['proposed_limit'],evidence['budget_unit'],budget_key=budget_key,conn=conn)
                answered = self.store.append_revision(pid,pending['object_id'],pending['revision'],{
                    **body,'state':'confirmed' if request.choice=='confirm' else 'pending' if withdrawing or undoing else 'declined',
                    **({'expires_at':self.clock()+self.ttl} if withdrawing or undoing else {})},'decision_service',conn=conn)
                receipt = self.store.create_object(pid,'human-receipt',{
                    'decision':_ref(answered),'target':body['target'],'purpose':body['purpose'],'choice':request.choice,
                    'human_actor':human.actor_id,'human_credential':human.credential_id,'verified_human_session':True,
                    # The owner's sentence (再拍一批 / 都不行 / a pick note) is what the agent reads to fix the card
                    # (AGENT_GUIDE step 5); found missing by the dry run.
                    'reason':request.reason,
                    'accepted':request.choice=='confirm','dependencies':[body['target']]},'decision_service',conn=conn)
                if selecting and request.selected_take is not None:
                    take = request.selected_take.model_dump()
                    self.store.create_object(pid, 'human-take-selection', {
                        'shot': body['target'], 'take': take, 'reason': request.reason,
                        'human_receipt': _ref(receipt), 'verified_human_session': True,
                        'dependencies': [body['target'], take, _ref(receipt)]}, 'decision_service', conn=conn)
                if body['purpose'] == 'take' and (selecting or withdrawing):
                    # The owner's picks changed, so a film offered before this decision no longer shows them.
                    for final in self.store.list_objects(pid, kind='decision-request', conn=conn):
                        if (final['author'] == 'decision_service' and final['body']['purpose'] == 'final'
                                and final['body']['state'] == 'pending'):
                            self.store.append_revision(pid, final['object_id'], final['revision'],
                                {**final['body'], 'state': 'superseded'}, 'decision_service', conn=conn)
                if withdrawing:
                    # 撤销选择 undoes this card's pick only: the shot keeps the pick it had from any other card
                    # (rehearsal walk r82: a withdrawn new pick also dropped the shot's earlier pick from the film).
                    restored = self._pick_without(pid, body['target']['object_id'], pending['object_id'], conn)
                    self.store.create_object(pid, 'human-take-selection', {
                        'shot': body['target'], 'take': restored, 'reason': request.reason,
                        'human_receipt': _ref(receipt), 'verified_human_session': True,
                        'dependencies': [body['target'], _ref(receipt), *([restored] if restored else [])]}, 'decision_service', conn=conn)
                if request.choice=='confirm' and body['purpose']=='final':
                    self.workflow.record_picture_lock(pid,ObjectRef.model_validate(body['evidence']['cut']),conn=conn)
                    evidence = body['evidence']
                    older = [*evidence.get('receipts', []), *evidence.get('finishing', []),
                             *[item['collection'] for item in evidence.get('qualification', [])], *evidence.get('take_receipts', [])]
                    self.store.create_object(pid,'final',{'media':body['target'],'human_receipt':_ref(receipt),
                        'review_receipts':evidence.get('receipts', []),'accepted':True,
                        'dependencies':[body['target'],_ref(receipt),*older]},'decision_service',conn=conn)
                return receipt
            return self.store.run_idempotent(f'{pid}:{human.credential_id}:human-decision',request.idempotency_key,
                                             request.model_dump(exclude={'csrf_token'}),save,conn=db)
