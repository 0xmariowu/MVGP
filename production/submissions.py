"""Atomic job authorization. No provider call belongs in this module.

The runtime config's cost policy (runtime.json `cost`) has operations.submit.<job_type>,
operations.complete-draft.<job_type> and operations.observe.<reader> cost profiles, and
operations.render-cut local policy.
Paid profiles declare mode, budget_key, budget_unit, estimated_cost, reservation, max_attempts
and three live_controls booleans. Missing policy is not a zero-price fallback.
Jobs point to immutable dispatch-intent records; workers CAS job revisions and
revalidate in the same transaction that commits dispatching, before network I/O.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from production import switches
from production.auth import AuthService, Principal
from production.contracts import (
    Contract,
    DomainError,
    ObjectRef,
    ObserveRequest,
    RenderCutRequest,
    SubmitRequest,
    content_hash,
    new_id,
)
from production.gates import Gates
from production.store import Store
from production.workflow import Workflow

SERVICE_AUTHOR = 'submission_service'
ReviewCheck = Callable[[str, dict[str, Any], dict[str, Any], sqlite3.Connection], dict[str, Any]]
Amount = Annotated[int, Field(ge=0, le=2**63-1)]
UNRESOLVED_STATES = frozenset({'dispatching', 'submitted', 'running', 'unknown'})
# a draft job type and the job type that completes its drafts at 1080p.
COMPLETIONS = {'fal_seedance_2_5': 'fal_seedance_2_5_complete'}


class LiveControls(Contract):
    pricing_verified: bool
    isolation_verified: bool
    account_limits_verified: bool


class CostPolicy(Contract):
    mode: Literal['fake', 'live']
    budget_key: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')] = 'legacy'
    budget_unit: Annotated[str, Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')]
    estimated_cost: Amount
    reservation: Annotated[int, Field(gt=0, le=2**63-1)]
    max_attempts: Annotated[int, Field(gt=0, le=1000)]
    live_controls: LiveControls

    @model_validator(mode='after')
    def conservative(self) -> CostPolicy:
        if self.mode == 'live' and self.budget_key == 'legacy':
            raise ValueError('Live execution requires an explicit native account bucket')
        if self.reservation < self.estimated_cost:
            raise ValueError('Reservation cannot be less than estimate')
        return self


class LocalPolicy(Contract):
    mode: Literal['local']
    max_attempts: Annotated[int, Field(gt=0, le=1000)]


def frozen_cost_matches(current: dict[str, Any], frozen: dict[str, Any], digest: str | None,
                        *, legacy_unhashed: bool = False) -> bool:
    """Compare without rewriting history or silently granting old live authority.

    Old fake intents hashed the exact dictionary without budget_key. Old fake
    review turns additionally predate cost_hash; only that narrow shape may omit
    it. Their immutable object digest still binds the stored cost. New records
    and all live records require the complete explicit account and original hash.
    """
    old_fake = (frozen.get('mode') == current.get('mode') == 'fake'
                and 'budget_key' not in frozen and current.get('budget_key') == 'legacy')
    if digest != content_hash(frozen) and not (digest is None and legacy_unhashed and old_fake):
        return False
    if frozen.get('mode') == 'live' and frozen.get('budget_key', 'legacy') == 'legacy':
        return False
    return current == frozen or (old_fake and current == {**frozen, 'budget_key':'legacy'})


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: obj[k] for k in ('object_id', 'revision', 'digest')}


class Submissions:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow, gates: Gates, *,
                 review_check: ReviewCheck | None = None,
                 live_enabled: bool = False, clock: Callable[[], float] = time.time) -> None:
        """review_check is a fake-only test seam, never an author request field (no AI review)."""
        self.store, self.auth, self.workflow, self.gates = store, auth, workflow, gates
        self.review_check, self.live_enabled, self.clock = review_check, live_enabled, clock

    def _policy(self, operation: str, route_key: str | None) -> dict[str, Any]:
        try:
            policy = self.workflow.config.section('execution_policy')['operations'][operation]
            if operation == 'render-cut':
                return LocalPolicy.model_validate(policy).model_dump()
            parsed = CostPolicy.model_validate(policy[route_key])
            if parsed.mode == 'live' and (not self.live_enabled or not all(parsed.live_controls.model_dump().values())):
                raise DomainError('forbidden', 'Live dispatch requires verified pricing, isolation, account controls and service enablement')
            return parsed.model_dump()
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError('missing_prerequisite', 'Execution cost/attempt policy is absent or invalid') from exc

    def _resolve(self, pid: str, ref: ObjectRef, db: sqlite3.Connection, *, current: bool = True) -> dict[str, Any]:
        obj = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
        if ref.digest is not None and obj['digest'] != ref.digest:
            raise DomainError('stale_input', 'Bound input digest differs')
        if current and self.store.get_object(pid, ref.object_id, conn=db)['revision'] != ref.revision:
            raise DomainError('revision_conflict', 'Bound input changed')
        return obj

    def _reviews(self, pid: str, candidate: dict[str, Any], gate: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
        """Judgment reviews are never a gate on generation (owner 2026-09-23; as at HF). Policy roles are recorded as advice only."""
        body = candidate['body']
        try:
            policy = self.workflow.review_policy(pid, 'submit', body['task'], body['method_id'], conn=db)
        except DomainError as exc:
            if exc.code != 'missing_prerequisite':
                raise
            policy = {'required_roles': []}
        return {'authorized': True, 'candidate': _ref(candidate), 'policy_hash': content_hash(policy), 'roles': [],
                'receipts': [], 'advisory_roles': list(policy['required_roles'])}

    def _stress_tested(self, pid: str, candidate: dict[str, Any], db: sqlite3.Connection) -> None:
        """With the owner's stress-test switch on, a narrative take needs ten stress takes of every identity image it
        references (ten in ten, briefs-clean/cully-hill-boys.txt:62-65).
        Counted by the reference image's sha256, not its tag: a state tag reuses its base still, and a rebuilt asset
        is a new image that starts again at 0. Runs on submit and batch create only, never when the worker re-checks
        a queued job, so turning the switch on never refuses takes already queued."""
        body = candidate['body']
        if body.get('task') != 'shot' or not switches.current(self.store, self.workflow.config, pid, db)['asset_stress_test']:
            return
        identity = ('person', 'place', 'state')
        needed: dict[str, set[str]] = {}
        for r in body['request'].get('references', []):
            if r.get('role') in identity and isinstance(r.get('sha256'), str):
                needed.setdefault(r['sha256'], set()).add(str(r.get('tag')))
        if not needed:
            return
        stress = {c['object_id']: {r['sha256'] for r in c['body'].get('request', {}).get('references', [])
                                   if r.get('role') in identity and isinstance(r.get('sha256'), str)}
                  for c in self.store.list_objects(pid, kind='candidate', conn=db)
                  if c['author'] == 'compiler_service' and c['body'].get('task') == 'stress'}
        intents = {i['object_id']: (i['body'].get('candidate') or {}).get('object_id')
                   for i in self.store.list_objects(pid, kind='dispatch-intent', conn=db)}
        counts = dict.fromkeys(needed, 0)
        for take in self.store.list_objects(pid, kind='media', conn=db):
            if take['author'] != 'worker_service':
                continue
            source = next((intents.get(d.get('object_id'), d.get('object_id')) for d in take['body'].get('dependencies', [])
                           if isinstance(d, dict) and intents.get(d.get('object_id'), d.get('object_id')) in stress), None)
            for sha in stress.get(source, ()):
                if sha in counts:
                    counts[sha] += 1
        short = sorted((' / '.join(sorted(needed[sha])), n) for sha, n in counts.items() if n < 10)
        if short:
            raise DomainError('missing_prerequisite', 'The stress-test switch is on: every asset image needs 10 stress takes first ('
                              + ', '.join(f'{tag} has {n}' for tag, n in short) + ')')

    def _generation(self, pid: str, candidate: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
        gate = self.gates.evaluate(pid, ObjectRef(**_ref(candidate)), conn=db)
        if not gate['mechanical_pass']:
            raise DomainError('rule_violation', 'Candidate is blocked by deterministic gates', repair=str(gate['blocking']))
        review = self._reviews(pid, candidate, gate, db)
        body = candidate['body']
        compilation = body['compilation']
        # The route profile is frozen into the intent: a later runtime.json change never
        # re-measures a take already sent. The gates checked its hash against the compilation.
        role = 'image_routes' if body['task'] in ('image', 'image-edit') else 'video_routes'
        return {'operation':'submit', 'target':body['target'], 'candidate':_ref(candidate),
                'release_id':body['release_id'], 'task':body['task'], 'method_id':body['method_id'],
                'route_key':body['request']['job_type'], 'request':body['request'],
                'route':{'profile_id':compilation['profile_id'], 'profile_hash':compilation['profile_hash']},
                'route_profile':self.workflow.config.section(role)['profiles'][compilation['profile_id']],
                'review':review, 'dependencies':[_ref(candidate), *review['receipts']]}

    def _completion(self, pid: str, take: dict[str, Any], db: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any]]:
        """The 1080p completion of a fal draft take: only the owner's current pick of its shot, before the draft
        id expires. Returns the dispatch data and the draft's maker origin, which stays
        the authority checked at dispatch."""
        from production.assembly import owner_picks
        proof = take['body'].get('provenance') if take['kind'] == 'media' and take['author'] == 'worker_service' else None
        if not isinstance(proof, dict) or 'intent' not in proof or 'completes' in take['body']:
            raise DomainError('invalid_input', 'Only a generated draft take can be completed')
        intent = self._resolve(pid, ObjectRef.model_validate(proof['intent']), db, current=False)
        body = intent['body']
        request = body.get('request') or {}
        if (intent['kind'] != 'dispatch-intent' or intent['author'] != SERVICE_AUTHOR or body.get('operation') != 'submit'
                or request.get('job_type') not in COMPLETIONS or request.get('params', {}).get('draft') is not True):
            raise DomainError('invalid_input', 'Only a fal draft take can be completed at 1080p')
        output = proof.get('provider_output') or {}
        draft_id, expires = output.get('draft_id'), output.get('draft_expires_at')
        if not isinstance(draft_id, str) or type(expires) is not int:
            raise DomainError('missing_prerequisite', 'The draft take has no draft id to complete')
        if expires <= self.clock():
            raise DomainError('stale_input', 'The draft is past its seven days; fal can no longer complete it')
        pick = owner_picks(self.store, pid, conn=db).get(body['target']['object_id'])
        if pick is None or pick['details']['take'] != _ref(take):
            raise DomainError('forbidden', "Only the owner's current pick of a shot is completed")
        params = request['params']
        completion = {'job_type': COMPLETIONS[request['job_type']], 'references': [],
                      'params': {'draft_id': draft_id, 'resolution': '1080p', 'duration': params['duration'],
                                 'aspect_ratio': params['aspect_ratio'], 'generate_audio': params['generate_audio']}}
        release_id = self.store.get_object(pid, pid, conn=db)['body']['release_id']
        return ({'operation': 'complete-draft', 'target': _ref(take), 'release_id': release_id, 'task': body['task'],
                 'method_id': body['method_id'], 'route_key': completion['job_type'], 'request': completion,
                 'route': body['route'], 'route_profile': body.get('route_profile'), 'dependencies': [_ref(take)]}, body['origin'])

    def complete_draft(self, actor: Principal, pid: str, take: ObjectRef, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Queue the 1080p completion of the owner's picked draft. A second call returns the earlier completion's job
        (in flight, done, unknown or possibly paid), even after a withdraw and a new pick of the same take. Only when
        every earlier completion provably cost nothing (no provider receipt, or its hold settled at zero) is another
        one queued, within the released allowance (a pick reaches 1080p or says why)."""
        with self.store._using(conn) as db:
            self.auth.authorize(actor, pid, 'complete-draft', conn=db)
            earlier = [i for i in self.store.list_objects(pid, kind='dispatch-intent', conn=db)
                       if i['author'] == SERVICE_AUTHOR and i['body'].get('operation') == 'complete-draft'
                       and i['body'].get('target', {}).get('object_id') == take.object_id]
            if earlier:
                ids = {i['object_id'] for i in earlier}
                jobs = [j for j in self.store.list_objects(pid, kind='job', conn=db) if j['body'].get('intent', {}).get('object_id') in ids]
                if not jobs:
                    raise DomainError('unknown_outcome', 'An earlier completion intent has no job; reconcile it before another paid effect')
                if not all(self._cost_nothing(pid, j, db) for j in jobs):
                    return max(jobs, key=lambda j: j['body'].get('attempt', 0))
            data, origin = self._completion(pid, self._resolve(pid, take, db), db)
            return self._enqueue(actor, pid, data, db, origin=origin)

    @staticmethod
    def _cost_nothing(pid: str, job: dict[str, Any], db: sqlite3.Connection) -> bool:
        """A failed job that never reached the provider, or whose hold settled at zero (a refusal at the queue, an
        upload that failed before the paid request). Unknown, in flight, succeeded or possibly charged: never."""
        body = job['body']
        if body.get('state') != 'failed':
            return False
        reservation = body.get('reservation_id')
        row = db.execute('SELECT state, actual FROM reservations WHERE project_id=? AND reservation_id=?',
                         (pid, reservation)).fetchone() if reservation else None
        return row is None or row['state'] == 'settled' and (row['actual'] == 0 or not body.get('remote_job_id'))

    def _media(self, pid: str, media: dict[str, Any]) -> dict[str, Any]:
        if media['kind'] != 'media':
            raise DomainError('invalid_media', 'Observation requires uploaded or service-generated media')
        self.gates.media.path_for(pid, media['object_id'], revision=media['revision'])
        body = media['body']
        return {'object_ref':_ref(media), 'sha256':body['sha256'], 'media_type':body['media_type'], 'bytes':body['size']}

    def _observation(self, pid: str, target: dict[str, Any], request: ObserveRequest, db: sqlite3.Connection) -> dict[str, Any]:
        media = self._media(pid, target)
        release_id = self.store.get_object(pid, pid, conn=db)['body']['release_id']
        try:
            routes = self.workflow.config.section('review_routes')
            profile_id = routes['role_routes']['observer']
            profile = routes['profiles'][profile_id]
            if (profile['role'] != 'observer' or request.reader not in profile['allowed_input_modalities']
                    or not media['media_type'].startswith(request.reader + '/')):
                raise DomainError('unsupported_route', 'Observer route does not support the selected media modality')
            cost = self._policy('observe', request.reader)
            if cost['mode'] == 'live' and not (profile.get('enablement', {}).get('live_enabled') is True
                    and profile.get('enablement', {}).get('isolation_verified') is True):
                raise DomainError('forbidden', 'Observer route has not been enabled for isolated live execution')
            if request.reader != 'video' and request.time_scale != 1.0:
                raise DomainError('invalid_input', 'Only visual video observation may declare retiming')
            if media['bytes'] > profile['limits']['max_input_bytes_per_turn']:
                raise DomainError('insufficient_context', 'Observation exceeds released input byte bound')
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError('unsupported_route', 'Observer capability profile is missing or invalid') from exc
        return {'operation':'observe', 'target':_ref(target), 'release_id':release_id,
                'task':'observe', 'method_id':None, 'route_key':request.reader,
                'route':{'profile_id':profile_id, 'profile_hash':content_hash(profile)},
                'request':{**request.model_dump(exclude={'idempotency_key','expected_revision','media_id'}), 'media':media},
                'dependencies':[_ref(target)]}

    def _cut(self, pid: str, target: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
        if target['kind'] != 'cut' or target['author'] != 'cut_service':
            raise DomainError('invalid_input', 'Rendering requires an exact service-authored cut manifest')
        if self.workflow.pinned_graph(pid, ObjectRef(**_ref(target)), conn=db)['stale']:
            raise DomainError('stale_input', 'Cut manifest or its inputs changed')
        release_id = self.store.get_object(pid,pid,conn=db)['body']['release_id']
        return {'operation':'render-cut', 'target':_ref(target), 'release_id':release_id,
                'task':'render-cut', 'method_id':None, 'route_key':None, 'route':{'kind':'local-render'},
                'request':{'cut':_ref(target), 'manifest':target['body']}, 'dependencies':[_ref(target)]}

    def _assert_lineage_resolved(self, pid: str, lineage: str, db: sqlite3.Connection, *,
                                 exclude_job_id: str | None = None, request_hash: str | None = None) -> None:
        """Siblings of the same service-issued batch never block each other.

        A take submission is guarded per exact request (bug hunt): an unresolved take never holds up a changed
        prompt of the same shot, as on Higgsfield where each generate click is its
        own job, while the same wire request -- even re-prepared under another
        candidate -- is never paid again until the first outcome is known.

        Each sibling is its own reserved take, not a retry of another, so a
        sibling in flight or with an unknown outcome (owner 2026-09-24: a stuck
        take must not hold up the rest of its shot) cannot duplicate it. Other
        batches and single submissions stay strict: any unresolved attempt on
        the target blocks them. Cancellation requests do not establish remote
        cancellation. Recheck here and at dispatch, since jobs may queue first.
        """
        siblings: set[str] = set()
        if exclude_job_id is not None:
            # Membership lives on the completed batch manifest, not its intents.
            for batch in self.store.list_objects(pid, kind='batch', conn=db):
                if batch['author'] == 'batch_service' and batch['body'].get('state') == 'queued':
                    members = {child['job']['object_id'] for child in batch['body']['children']}
                    if exclude_job_id in members:
                        siblings = members
                        break
        intent_ids = {obj['object_id'] for obj in self.store.list_objects(pid, kind='dispatch-intent', conn=db)
                      if obj['author'] == SERVICE_AUTHOR and obj['body'].get('lineage') == lineage
                      and (request_hash is None or content_hash(obj['body'].get('request')) == request_hash)}
        for job in self.store.list_objects(pid, kind='job', conn=db):
            if (job['object_id'] != exclude_job_id and job['author'] in (SERVICE_AUTHOR, 'worker_service')
                    and job['body'].get('intent', {}).get('object_id') in intent_ids
                    and job['body'].get('state') in UNRESOLVED_STATES):
                if job['object_id'] in siblings:
                    continue
                raise DomainError('unknown_outcome', 'A prior target attempt is still in flight or unresolved; finish or reconcile it before another paid effect')

    def _enqueue(self, actor: Principal, pid: str, data: dict[str, Any], db: sqlite3.Connection, *,
                 origin: dict[str, Any] | None = None) -> dict[str, Any]:
        cost = self._policy(data['operation'], data['route_key'])
        # Keep target lineage stable for in-flight guards and historical intents.
        # Generation allowance counts only the immutable candidate version;
        # observation and rendering retain their target-scoped allowances.
        lineage = content_hash({'project_id':pid, 'operation':data['operation'], 'target_id':data['target']['object_id']})
        previous = [o for o in self.store.list_objects(pid, kind='dispatch-intent',conn=db)
                    if o['author'] == SERVICE_AUTHOR and o['body'].get('lineage') == lineage]
        if data['operation'] == 'submit':
            previous = [o for o in previous
                        if o['body'].get('candidate', {}).get('object_id') == data['candidate']['object_id']]
            if data.get('batch') is not None:
                # a reshoot is a new batch of the same candidate, as HF re-fires identical
                # prompts in 42–52% of batches; the allowance counts inside this batch. Single submits keep the
                # candidate's allowance. The in-flight guard below still holds a new batch until earlier takes of
                # the same request are resolved, and the budget reserves every take.
                previous = [o for o in previous if o['body'].get('batch') == data['batch']]
        elif data['operation'] == 'render-cut':
            # Each re-cut after a pick change is a new cut revision with its own local render allowance
            # (dry run: three earlier renders of the film had exhausted it for good on both films).
            previous = [o for o in previous if o['body'].get('target', {}).get('revision') == data['target']['revision']]
        if len(previous) >= cost['max_attempts']:
            subject = 'Candidate version' if data['operation'] == 'submit' else 'Creative target'
            raise DomainError('attempt_limit', f'{subject} exhausted its released attempt allowance')
        self._assert_lineage_resolved(pid, lineage, db, request_hash=(
            content_hash(data['request']) if data['operation'] == 'submit' else None))
        origin = origin or {'actor_id':actor.actor_id, 'credential_id':actor.credential_id, 'role':actor.role}
        intent = self.store.create_object(pid,'dispatch-intent',{**data,'cost':cost,'cost_hash':content_hash(cost),
            'origin':origin,'lineage':lineage,'attempt':len(previous)+1},SERVICE_AUTHOR,conn=db)
        job_id = new_id('job')
        reservation_id = None
        if cost['mode'] != 'local':
            reservation_id = intent['object_id']
            self.store.reserve(pid,reservation_id,cost['reservation'],cost['budget_unit'],budget_key=cost['budget_key'],object_id=intent['object_id'],conn=db)
        job = self.store.create_object(pid,'job',{'state':'queued','intent':_ref(intent),'reservation_id':reservation_id,
            'attempt':len(previous)+1,'dependencies':[_ref(intent)]},SERVICE_AUTHOR,object_id=job_id,conn=db)
        self.store.append_event(pid,'job.queued',{'job':_ref(job),'intent':_ref(intent),'origin':origin},conn=db)
        return job

    def _mutate(self, actor: Principal, pid: str, request: SubmitRequest | ObserveRequest | RenderCutRequest,
                operation: str, conn: sqlite3.Connection | None, *, batch: str | None = None) -> dict[str, Any]:
        with self.store._using(conn) as db:
            self.auth.authorize(actor,pid,operation,conn=db)
            def enqueue(active: sqlite3.Connection) -> dict[str, Any]:
                if isinstance(request, SubmitRequest):
                    candidate = self._resolve(pid,ObjectRef(object_id=request.candidate_id,revision=request.expected_revision),active)
                    self._stress_tested(pid,candidate,active)
                    data = self._generation(pid,candidate,active)
                    if batch is not None:
                        data['batch'] = batch
                elif isinstance(request, ObserveRequest):
                    target = self._resolve(pid,ObjectRef(object_id=request.media_id,revision=request.expected_revision),active)
                    data = self._observation(pid,target,request,active)
                else:
                    if request.expected_revision != request.cut.revision:
                        raise DomainError('revision_conflict','Render guard differs from cut revision')
                    data = self._cut(pid,self._resolve(pid,request.cut,active),active)
                return self._enqueue(actor,pid,data,active)
            return self.store.run_idempotent(f'{actor.actor_id}:{actor.credential_id}:{pid}:{operation}',
                request.idempotency_key,request.model_dump(),enqueue,conn=db)

    def submit(self, actor: Principal, project_id: str, request: SubmitRequest, *, conn: sqlite3.Connection | None = None,
               batch: str | None = None) -> dict[str, Any]:
        """`batch` is set only by the batch service (a server-made batch id, never a request field): each batch of a
        candidate has the candidate's released allowance."""
        return self._mutate(actor,project_id,request,'submit',conn,batch=batch)

    def observe(self, actor: Principal, project_id: str, request: ObserveRequest, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        return self._mutate(actor,project_id,request,'observe',conn)

    def render_cut(self, actor: Principal, project_id: str, request: RenderCutRequest, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        return self._mutate(actor,project_id,request,'render-cut',conn)

    def revalidate(self, project_id: str, job_ref: ObjectRef, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Worker-only: call inside its fenced dispatch transaction, returning intent.

        This method neither claims a lease nor transitions state. Do not call it,
        commit, and later treat that old result as authority to dispatch. Origin
        credentials are re-read from trusted storage so process restart needs no
        saved bearer token or reconstructed client principal.
        """
        with self.store._using(conn) as db:
            job = self._resolve(project_id,job_ref,db)
            if job['kind'] != 'job' or job['author'] not in (SERVICE_AUTHOR,'worker_service') or job['body']['state'] != 'queued':
                raise DomainError('forbidden','Only a current queued service job can obtain dispatch authority')
            intent = self._resolve(project_id,ObjectRef.model_validate(job['body']['intent']),db)
            if intent['kind'] != 'dispatch-intent' or intent['author'] != SERVICE_AUTHOR or intent['revision'] != 1:
                raise DomainError('forbidden','Dispatch intent is not immutable service output')
            body = intent['body']
            self._assert_lineage_resolved(project_id, body['lineage'], db, exclude_job_id=job['object_id'],
                                          request_hash=(content_hash(body['request']) if body['operation'] == 'submit' else None))
            origin = body['origin']
            self.auth.require_agent_origin(project_id, origin, conn=db)
            if self.store.get_object(project_id,project_id,conn=db)['body']['release_id'] != body['release_id']:
                raise DomainError('release_mismatch','Project release changed before dispatch')
            if body['method_id']:
                self.workflow.config.require_method(body['method_id'], task=body['task'])
            cost = self._policy(body['operation'],body['route_key'])
            if not frozen_cost_matches(cost, body['cost'], body['cost_hash']):
                raise DomainError('release_mismatch','Frozen execution cost policy differs')
            if body['operation'] == 'submit':
                current = self._generation(project_id,self._resolve(project_id,ObjectRef(**body['candidate']),db),db)
            elif body['operation'] == 'observe':
                target = self._resolve(project_id,ObjectRef(**body['target']),db)
                observed = body['request']
                req = ObserveRequest(idempotency_key='revalidation',expected_revision=target['revision'],media_id=target['object_id'],
                    **{k:observed[k] for k in ('questions','reader','source_offset_seconds','time_scale')})
                current = self._observation(project_id,target,req,db)
            elif body['operation'] == 'render-cut':
                current = self._cut(project_id,self._resolve(project_id,ObjectRef(**body['target']),db),db)
            elif body['operation'] == 'complete-draft':
                # Still the owner's current pick, still inside the draft's seven days.
                current, _ = self._completion(project_id,self._resolve(project_id,ObjectRef(**body['target']),db),db)
            else:
                raise DomainError('forbidden','Unknown dispatch operation')
            if body['operation'] == 'submit' and current['review'] != body['review']:
                raise DomainError('review_required', 'Queued generation no longer has its exact frozen review authority')
            if any(current[key] != body[key] for key in ('request','route','target','release_id','dependencies')):
                raise DomainError('stale_input','Frozen dispatch inputs changed')
            if cost['mode'] != 'local':
                budget = self.store.budget(project_id,budget_key=cost['budget_key'],conn=db)
                reservation = db.execute('SELECT * FROM reservations WHERE project_id=? AND reservation_id=?',
                    (project_id,job['body']['reservation_id'])).fetchone()
                if (not reservation or reservation['state'] != 'held' or reservation['object_id'] != intent['object_id']
                        or reservation['budget_key'] != cost['budget_key']
                        or reservation['amount'] != cost['reservation'] or budget['unit'] != cost['budget_unit']
                        or budget['reserved'] + budget['spent'] > budget['ceiling']):
                    raise DomainError('budget_exceeded','Dispatch reservation is absent, uncertain or incompatible')
            return intent
