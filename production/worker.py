"""Persistent generation, observation and deterministic local-task worker.

Construct services through build_worker with an explicit private operator config.
No test fixtures, dynamic factories or author-side provider keys in bootstrap.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from pathlib import Path
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field, ValidationError

from production import listing_reconcile, review_http
from production.assembly import assemble, completions
from production.auth import AuthService, Principal
from production.batches import Batches
from production.contracts import (
    Contract,
    CutRequest,
    DecisionRequest,
    DomainError,
    ObjectRef,
    RenderCutRequest,
    content_hash,
)
from production.cuts import Cuts
from production.decisions import Decisions
from production.frame_evidence import FrameEvidence, FramePolicy
from production.gates import Gates
from production.jobs import Jobs, _http
from production.provider_apilio import ApilioImages
from production.provider_fal import FalSeedance
from production.provider_hf import ExecutablePin, HFProvider
from production.provider_router import ProviderRouter
from production.provider_types import ResolvedReference
from production.reader import Reader
from production.reviews import Reviews
from production.runtime_config import RuntimeConfig
from production.runtime_storage import (
    RuntimeStorage,
    StorageConfiguration,
    open_storage,
)
from production.shoot import AutoShoot, Shoot
from production.submissions import Submissions
from production.workflow import Workflow


def _ref(obj: dict[str, Any]) -> ObjectRef:
    return ObjectRef(**{k:obj[k] for k in ('object_id','revision','digest')})


class AutoComplete:
    """The owner's picked fal draft is completed to 1080p ten minutes after the pick.

    The wait lets a changed mind cost nothing: a pick replaced inside ten minutes is never completed. Each draft is
    completed at most once ever (Submissions.complete_draft), an expired draft is skipped (it plays at 480p), and
    the pick's time is its record in the store, so a worker restart does not restart the wait.
    """
    DELAY_SECONDS = 600
    # a pick that cannot be completed says why on the desk instead of 正片生成中 forever.
    STOPPED = 'completion.stopped'

    def __init__(self, principal: Principal, submissions: Submissions, *, clock: Any = time.time) -> None:
        if principal.role != 'worker':
            raise ValueError('Draft completion runs under the worker credential')
        self.principal, self.submissions, self.store, self.clock = principal, submissions, submissions.store, clock

    def advance(self, pid: str) -> str:
        from datetime import datetime, timezone

        from production.queries import creation_order
        now = self.clock()
        due = []
        with self.store.transaction(write=False) as db:
            human = [o for o in self.store.list_objects(pid, kind='human-take-selection', conn=db)
                     if o['author'] == 'decision_service' and o['body'].get('verified_human_session') is True]
            latest: dict[str, dict[str, Any]] = {}
            order = creation_order(db, pid, [o['object_id'] for o in human])
            for o in sorted(human, key=lambda o: order[o['object_id']]):
                latest[(o['body'].get('shot') or {}).get('object_id')] = o
            done = completions(self.store, pid, conn=db)
            for selection in latest.values():
                take = selection['body'].get('take')
                # A failed completion is looked at again: complete_draft sends another only if it cost nothing.
                if not isinstance(take, dict) or done.get(take.get('object_id'), {}).get('state', 'failed') != 'failed':
                    continue
                media = self.store.get_object(pid, take['object_id'], revision=take['revision'], conn=db)
                output = (media['body'].get('provenance') or {}).get('provider_output') or {}
                if not output.get('draft_id') or output.get('draft_expires_at', 0) <= now:
                    continue
                row = db.execute('SELECT created_at FROM revisions WHERE project_id=? AND object_id=? AND revision=1',
                                 (pid, selection['object_id'])).fetchone()
                picked = datetime.strptime(row[0], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc).timestamp()
                if now - picked >= self.DELAY_SECONDS:
                    due.append(take)
        failed = []
        for take in due:
            try:
                self.submissions.complete_draft(self.principal, pid, ObjectRef.model_validate(take))
            except DomainError as exc:  # one take's refusal (the pick just changed) never holds up the others
                failed.append(exc.code)
                if exc.code in ('budget_exceeded', 'attempt_limit'):
                    self._stopped(pid, take, exc)
        if failed:
            return 'blocked:' + ','.join(sorted(set(failed)))
        return f'completing:{len(due)}' if due else 'idle'

    def _stopped(self, pid: str, take: dict[str, Any], exc: DomainError) -> None:
        """One visible record per take and reason: the desk shows 预算不够，正片没做 / 正片失败，已重试 from it."""
        with self.store.transaction() as db:
            last = db.execute("SELECT body FROM events WHERE project_id=? AND kind=? AND json_extract(body,'$.take.object_id')=? "
                              "ORDER BY sequence DESC LIMIT 1", (pid, self.STOPPED, take.get('object_id'))).fetchone()
            if last is not None and json.loads(last[0]).get('code') == exc.code:
                return
            self.store.append_event(pid, self.STOPPED, {'take': take, 'code': exc.code, 'reason': exc.message}, conn=db)


class AutoFilm:
    """Picks become the film (owner 2026-09-24 "应该是我选好的片子自动可以去全片那里看", "不用你剪").

    Once every shot of the film has a current human pick, the platform cuts the picks in shot order, renders the cut
    locally and asks the owner for the final (no finishing measurements; HF has none). It never confirms a final,
    never acts without a human pick and never spends provider money. Each call advances at most one stage and the
    records are the only state, so a restart resumes where the records stop. A pick change supersedes a pending
    final that no longer shows the current picks.
    """
    MAKING = ('queued', 'dispatching', 'submitted', 'running', 'unknown')

    def __init__(self, principal: Principal, cuts: Cuts, submissions: Submissions, decisions: Decisions) -> None:
        if principal.role != 'agent' or cuts.store is not submissions.store or decisions.store is not cuts.store:
            raise ValueError('The film service needs its own agent credential and one shared store')
        self.principal, self.cuts, self.submissions, self.decisions = principal, cuts, submissions, decisions
        self.store = cuts.store

    def _render_states(self, pid: str, cut_ref: dict[str, Any], db: sqlite3.Connection) -> list[str]:
        states = []
        for job in self.store.list_objects(pid, kind='job', conn=db):
            intent_ref = job['body'].get('intent')
            if not isinstance(intent_ref, dict):
                continue
            intent = self.store.get_object(pid, intent_ref['object_id'], revision=intent_ref.get('revision'), conn=db)
            if intent['body'].get('operation') == 'render-cut' and intent['body'].get('target') == cut_ref:
                states.append(job['body'].get('state'))
        return states

    def advance(self, pid: str) -> str:
        with self.store.transaction(write=False) as db:
            assembly = assemble(self.store, pid, conn=db, now=self.submissions.clock())
            cuts = [o for o in self.store.list_objects(pid, kind='cut', conn=db) if o['author'] == 'cut_service']
            cut = cuts[0] if cuts else None
            project = self.store.get_object(pid, pid, conn=db)
            finals = [o for o in self.store.list_objects(pid, kind='decision-request', conn=db)
                      if o['author'] == 'decision_service' and o['body'].get('purpose') == 'final']
        plan = [(s['take'], s['start_seconds'], s['end_seconds']) for s in assembly['segments']]
        # A cut made under an earlier release cannot be finished or offered under this one: recut it.
        same = (cut is not None and assembly['complete'] and cut['body'].get('release_id') == project['body'].get('release_id')
                and [(s['take'], s['start_seconds'], s['end_seconds']) for s in cut['body']['segments']] == plan)
        current = {k: cut[k] for k in ('object_id', 'revision', 'digest')} if same and cut else None
        stale = [f for f in finals if f['body']['state'] == 'pending' and f['body']['evidence'].get('cut') != current]
        if stale:
            with self.store.transaction() as db:
                for request in stale:
                    fresh = self.store.get_object(pid, request['object_id'], conn=db)
                    if fresh['body']['state'] == 'pending':
                        self.store.append_revision(pid, fresh['object_id'], fresh['revision'],
                                                   {**fresh['body'], 'state': 'superseded'}, 'decision_service', conn=db)
        if not assembly['complete']:
            return 'waiting-for-completions' if assembly['completing'] and not assembly['missing'] else 'waiting-for-picks'
        if not same or cut is None:
            guard = cut['revision'] if cut else project['revision']
            self.cuts.create(self.principal, pid, CutRequest(
                idempotency_key=f"auto-film-{guard}-{assembly['picks_hash'][:32]}", expected_revision=guard,
                segments=assembly['segments'], sound_inputs=assembly['sound_inputs'], intent=assembly['intent']))
            return 'cut-created'
        assert current is not None
        with self.store.transaction(write=False) as db:
            rendered = [m for m in self.store.list_objects(pid, kind='media', conn=db)
                        if m['author'] == 'cut_service' and m['body'].get('source_cut') == current]
            states = [] if rendered else self._render_states(pid, current, db)
        if not rendered:
            if any(state in self.MAKING for state in states):
                return 'rendering'
            if states:
                return 'render-failed'  # no automatic retry loop; a pick change or an operator starts over
            self.submissions.render_cut(self.principal, pid, RenderCutRequest(
                idempotency_key=f"auto-render-{current['object_id']}-{current['revision']}",
                expected_revision=current['revision'], cut=ObjectRef(**current)))
            return 'render-requested'
        media = rendered[-1]
        media_ref = {k: media[k] for k in ('object_id', 'revision', 'digest')}
        mine = [f for f in finals if f['body'].get('target') == media_ref]
        if any(f['body']['state'] == 'confirmed' for f in mine):
            return 'confirmed'
        if any(f['body']['state'] == 'pending' and f['body']['expires_at'] > self.decisions.clock() for f in mine):
            return 'waiting-for-owner'
        if any(f['body']['state'] == 'declined' for f in mine):
            return 'declined'  # the owner said no to this film; a new pick makes a new one
        self.decisions.request(self.principal, pid, DecisionRequest(
            idempotency_key=f"auto-final-{media['object_id']}-{len(mine)}", expected_revision=media['revision'],
            target=ObjectRef(**media_ref), purpose='final', rationale='按你选的各条自动拼好的成片。'))
        return 'final-requested'


class Worker:
    def __init__(self, jobs: Jobs, provider: ProviderRouter | None, principal: Principal, project_ids: list[str], *,
                 concurrency: int = 1, reader: Reader | None = None, cuts: Cuts | None = None, ffmpeg: Path | None = None, ffprobe: Path | None = None,
                 film: AutoFilm | None = None, film_interval: float = 10.0,
                 create_concurrency: int | None = None, shoot: AutoShoot | None = None,
                 complete: AutoComplete | None = None) -> None:
        if type(concurrency) is not int or not 1<=concurrency<=12:
            raise ValueError('A bounded concurrency of 1..12 is required')
        if create_concurrency is not None and (type(create_concurrency) is not int or not 1<=create_concurrency<=concurrency):
            raise ValueError('Create concurrency must be 1..concurrency')
        if provider is not None and jobs.lease_seconds <= 2*provider.timeout+5:
            raise ValueError('Worker lease must cover CLI version/command deadlines plus publication margin')
        if reader is not None and reader.jobs is not jobs or cuts is not None and cuts.store is not jobs.store:
            raise ValueError('Worker handlers must share the same service authority')
        if film is not None and film.store is not jobs.store:
            raise ValueError('The film service must share the same store')
        self.film,self.film_interval=film,film_interval
        if shoot is not None and shoot.store is not jobs.store:
            raise ValueError('The shoot service must share the same store')
        self.shoot=shoot
        if complete is not None and complete.store is not jobs.store:
            raise ValueError('Draft completion must share the same store')
        self.complete=complete
        self.complete_status: dict[str, str] = {}
        self._complete_due: dict[str, float] = {}
        self.shoot_status: dict[str, str] = {}
        self._shoot_due: dict[str, float] = {}
        self.film_status: dict[str, str] = {}
        self._film_due: dict[str, float] = {}
        self.reader,self.cuts=reader,cuts
        self.ffmpeg=(ffmpeg or (cuts.ffmpeg if cuts else Path(shutil.which('ffmpeg') or '/unavailable/ffmpeg'))).resolve()
        self.ffprobe=(ffprobe or Path(shutil.which('ffprobe') or '/unavailable/ffprobe')).resolve()
        self.runtime_storage: RuntimeStorage | None = None
        self.jobs,self.provider,self.principal=jobs,provider,principal
        self.projects,self.concurrency=tuple(dict.fromkeys(project_ids)),concurrency
        # with no configured list the worker follows its credential's scope (every project for
        # an all-projects credential), re-read every SCOPE_SECONDS, so a new project needs no config change or restart.
        self._dynamic = not self.projects
        self._scope_due = 0.0
        self.create_concurrency=create_concurrency or concurrency
        self._slots=threading.BoundedSemaphore(concurrency)
        self._scan_after: tuple[int, str] = (-1, '')
        self._listing_next: dict[str, float] = {}
        self.poll_interval=1.0
        for pid in self.projects:
            jobs.auth.authorize(principal,pid,'dispatch')
        self._refresh_projects()

    SCOPE_SECONDS = 30.0

    def _refresh_projects(self) -> None:
        if not self._dynamic or time.monotonic() < self._scope_due:
            return
        self._scope_due = time.monotonic() + self.SCOPE_SECONDS
        current = tuple(self.jobs.auth.dispatch_scope(self.principal))
        for pid in current:
            if pid not in self.projects:
                print(f'worker scope + {pid}', file=sys.stderr, flush=True)
        self.projects = current

    @property
    def configuration_status(self) -> dict[str, Any]:
        return {'storage': self.runtime_storage.readiness() if self.runtime_storage else
                    {'mode':'injected'},
                'generation_adapter':('fake' if self.provider.fake else 'live-enabled' if self.provider.live_enabled else 'live-disabled') if self.provider else 'unconfigured',
                'observation_adapter':('fake' if self.reader.fake else 'released-native-live-disabled-unless-priced') if self.reader else 'unconfigured',
                'review_http_healthy':review_http.is_healthy(),
                'supported_operations':self.operations,'concurrency':self.concurrency,'create_concurrency':self.create_concurrency}

    def close(self) -> None:
        if self.runtime_storage is not None:
            self.runtime_storage.close()

    @property
    def operations(self) -> list[str]:
        return (['submit','complete-draft'] if self.provider else [])+(['observe'] if self.reader else [])+(['render-cut'] if self.cuts else [])

    def _intent(self, pid: str, job: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self.jobs.store._using(conn, write=False) as db:
            self.jobs.auth.authorize(self.principal,pid,'dispatch',conn=db)
            ref=ObjectRef.model_validate(job['body']['intent'])
            intent=self.jobs.store.get_object(pid,ref.object_id,revision=ref.revision,conn=db)
            if (intent['kind']!='dispatch-intent' or intent['revision']!=1 or intent['author']!='submission_service'
                    or intent['digest']!=ref.digest):
                raise DomainError('forbidden','Job lacks immutable service dispatch intent')
            return intent

    def _references(self, pid: str, request: dict[str, Any]) -> list[ResolvedReference]:
        resolved=[]
        for entry in request['references']:
            ref=ObjectRef.model_validate(entry['object_ref'])
            media=self.jobs.store.get_object(pid,ref.object_id,revision=ref.revision)
            if (media['kind']!='media' or media['digest']!=ref.digest or media['body']['sha256']!=entry['sha256']
                    or media['body']['media_type']!=entry['media_type']):
                raise DomainError('invalid_media','Frozen reference does not match verified media')
            resolved.append(ResolvedReference(ref.model_dump(),self.jobs.media.path_for(pid,ref.object_id,revision=ref.revision),
                                              entry['sha256'],entry['media_type']))
        return resolved

    def _owned(self, pid: str, job_id: str, fence: int) -> dict[str, Any]:
        """Refresh only the same lease after benign cancel_requested revisions."""
        with self.jobs.store.transaction(write=False) as db:
            self.jobs.auth.authorize(self.principal,pid,'record-provider-result',conn=db)
            current=self.jobs.store.get_object(pid,job_id,conn=db)
            return self.jobs._fenced(self.principal,pid,_ref(current),fence,db)

    @staticmethod
    def _outcome(job: dict[str, Any], action: str, error: str | None = None) -> dict[str, Any]:
        result={'job_id':job['object_id'],'action':action,'state':job['body']['state']}
        if error:
            result['error_code']=error
        return result

    def _error(self, pid: str, job: dict[str, Any], fence: int | None, action: str, code: str, after_dispatch: bool) -> dict[str, Any]:
        try:
            if after_dispatch and fence is not None:
                current=self._owned(pid,job['object_id'],fence)
                reason=code if code in ('unknown_outcome','provider_failure','invalid_media','unsupported_route','forbidden') else 'provider_failure'
                job=self.jobs.record_unknown(self.principal,pid,_ref(current),fence,reason)
            else:
                with self.jobs.store.transaction() as db:
                    self.jobs.auth.authorize(self.principal,pid,'dispatch',conn=db)
                    self.jobs.store.append_event(pid,'worker.blocked',{'job_id':job['object_id'],'error_code':code},conn=db)
        except DomainError:
            # An expired fence cannot rewrite another worker's outcome, even to
            # report failure. Its durable dispatch journal remains reconcilable.
            pass
        with self.jobs.store.transaction(write=False) as db:
            self.jobs.auth.authorize(self.principal,pid,'dispatch',conn=db)
            job=self.jobs.store.get_object(pid,job['object_id'],conn=db)
        return self._outcome(job,action,code)

    def _local_attempts(self, pid: str, job: dict[str, Any]) -> int:
        return sum(o['author']=='worker_service' and o['body']['job_id']==job['object_id']
                   for o in self.jobs.store.list_objects(pid,kind='provider-attempt'))

    def _local_retry(self, pid: str, job: dict[str, Any], intent: dict[str, Any], *, fence: int | None = None) -> dict[str, Any]:
        """Only deterministic local rendering may repeat an expired attempt."""
        with self.jobs.store.transaction() as db:
            current=self.jobs._job(self.principal,pid,_ref(job),db,'dispatch')
            body=intent['body']
            if body['operation']!='render-cut' or body['cost']['mode']!='local' or current['body']['intent']!=_ref(intent).model_dump():
                raise DomainError('forbidden','Paid work cannot enter local retry recovery')
            if fence is not None:
                self.jobs._fenced(self.principal,pid,_ref(current),fence,db)
            elif (current['body'].get('lease') or {}).get('expires_at',0)>self.jobs.clock():
                raise DomainError('revision_conflict','Local render still has an active owner')
            if current['body']['state'] not in ('dispatching','unknown') or current['body'].get('remote_job_id'):
                raise DomainError('forbidden','Local recovery requires an interrupted local attempt')
            state='cancelled' if current['body'].get('cancel_requested') else 'failed' if self._local_attempts(pid,current)>=body['cost']['max_attempts'] else 'queued'
            return self.jobs._write(pid,current,{'state':state,'lease':None,'local_retry_count':current['body'].get('local_retry_count',0)+1},'worker.local_recovery',db)

    def _derivative(self, pid: str, job: dict[str, Any], fence: int, intent: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        request=intent['body']['request']
        if request['time_scale']==1.0 or job['body'].get('visual_derivative'):
            return job
        if request['time_scale']!=4.0 or profile['retiming_policy']['optional_silent_visual_derivative_factor']!=4:
            raise DomainError('unsupported_route','Only the released optional silent 4x derivative is supported')
        source=ObjectRef.model_validate(request['media']['object_ref'])
        media=self.jobs.store.get_object(pid,source.object_id,revision=source.revision)
        duration=media['body']['probe']['duration']
        limit=min(self.jobs.media.max_bytes,profile['limits']['max_input_bytes_per_turn']-media['body']['size'])
        if duration*5>profile['limits']['max_submitted_video_seconds_total'] or limit<=0:
            raise DomainError('insufficient_context','Original plus derivative exceeds the released observation limit')
        path=self.jobs.media.path_for(pid,source.object_id,revision=source.revision)
        with tempfile.TemporaryDirectory(prefix='mvgp-observe-') as tmp:
            target=Path(tmp)/'silent4.mp4'
            try:
                subprocess.run([str(self.ffmpeg),'-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(path),
                    '-map','0:v:0','-an','-map_chapters','-1','-map_metadata','-1','-c:v','copy',
                    '-bsf:v','setts=pts=4*PTS:dts=4*DTS:duration=4*DURATION','-fs',str(limit),str(target)],check=True,
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=min(120,self.jobs.lease_seconds-5))
            except (OSError,subprocess.SubprocessError) as exc:
                raise DomainError('invalid_media','Silent visual derivative failed before paid dispatch') from exc
            def publish(metadata: dict[str, Any]) -> dict[str, Any]:
                probe=metadata['probe']
                if (probe['has_audio'] or not probe['has_video'] or abs(probe['duration']-4*duration)>0.25
                        or (probe['width'],probe['height'])!=(media['body']['probe']['width'],media['body']['probe']['height'])):
                    raise DomainError('invalid_media','Derivative is not the declared silent 4x original')
                with self.jobs.store.transaction() as db:
                    current=self.jobs._fenced(self.principal,pid,_ref(job),fence,db)
                    derived=self.jobs.store.create_object(pid,'media',{**metadata,'dependencies':[source.model_dump()],
                        'time_map':{'time_scale':4.0,'source_offset_seconds':request['source_offset_seconds']}},'worker_service',conn=db)
                    return self.jobs._write(pid,current,{'visual_derivative':_ref(derived).model_dump()},'observation.derivative_prepared',db)
            with target.open('rb') as stream:
                return self.jobs.media.put(pid,iter(lambda:stream.read(1048576),b''),'video/mp4','worker_service',
                    derivative_of=source.model_dump(),publish=publish)

    @staticmethod
    def _frame_policy(profile: dict[str, Any]) -> dict[str, Any] | None:
        policy=profile.get('frame_evidence')
        if policy is None or isinstance(policy,dict) and policy.get('enabled') is False:
            return None  # Legacy profiles cannot claim frame evidence.
        try:
            if (policy['enabled'] is not True or policy['authority']!='LOCAL' or policy['source']!='original-media'
                    or policy['timestamp_origin']!='format.start_time' or policy['exhaustive'] is not False
                    or policy['replaces_original_video'] is not False
                    or set(policy['limits'])!={field.name for field in fields(FramePolicy)}):
                raise ValueError('Invalid local evidence policy')
            FramePolicy(**policy['limits'])
        except (KeyError,TypeError,ValueError) as exc:
            raise DomainError('unsupported_route','Explicit valid local frame policy required') from exc
        return policy

    def _check_frames(self, pid: str, pack: dict[str, Any], intent: dict[str, Any],
                      policy: dict[str, Any], db: sqlite3.Connection) -> list[dict[str, Any]]:
        source=intent['body']['request']['media']
        if (pack.get('source')!=source['object_ref'] or pack.get('source_sha256')!=source['sha256']
                or pack.get('release_id')!=intent['body']['release_id'] or pack.get('policy_hash')!=content_hash(policy)
                or pack.get('exhaustive') is not False or pack.get('replaces_original_video') is not False
                or not 1<=len(pack.get('frames',[]))<=policy['limits']['max_frames']):
            raise DomainError('invalid_media','Prepared frame evidence does not bind the released original')
        self.jobs.media.path_for(pid,source['object_ref']['object_id'],revision=source['object_ref']['revision'])
        for frame in pack['frames']:
            ref=ObjectRef.model_validate(frame['media'])
            media=self.jobs.store.get_object(pid,ref.object_id,revision=ref.revision,conn=db)
            metadata={key:value for key,value in frame.items() if key!='media'}
            if (_ref(media)!=ref or media['kind']!='media' or media['author']!='worker_service'
                    or media['body']['media_type']!='image/png' or media['body']['derivative_of']!=source['object_ref']
                    or media['body'].get('frame_evidence')!=metadata or frame['source_sha256']!=source['sha256']):
                raise DomainError('invalid_media','Prepared frame lacks exact immutable source lineage')
            self.jobs.media.path_for(pid,ref.object_id,revision=ref.revision)
        return pack['frames']

    def _prepare_frames(self, pid: str, job: dict[str, Any], fence: int,
                        intent: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        policy=self._frame_policy(profile)
        if policy is None:
            return job
        limits=FramePolicy(**policy['limits'])
        if self.jobs.lease_seconds<=limits.timeout_seconds+5:
            raise DomainError('invalid_input','Observation lease must cover bounded frame extraction and publication')
        with self.jobs.store.transaction() as db:
            current=self.jobs.store.get_object(pid,job['object_id'],conn=db)
            current=self.jobs._fenced(self.principal,pid,_ref(current),fence,db)
            self.jobs.submissions.revalidate(pid,_ref(current),conn=db)
            if current['body'].get('prepared_frames'):
                self._check_frames(pid,current['body']['prepared_frames'],intent,policy,db)
                return current
        job=self.jobs.renew(self.principal,pid,_ref(current),fence)
        source=ObjectRef.model_validate(intent['body']['request']['media']['object_ref'])
        extractor=FrameEvidence(self.jobs.media,policy=limits,ffmpeg=self.ffmpeg,ffprobe=self.ffprobe)
        samples=extractor.extract(pid,source)
        if not 1<=len(samples)<=limits.max_frames or any(sample.source_sha256!=intent['body']['request']['media']['sha256'] for sample in samples):
            raise DomainError('invalid_media','Extractor did not supply a complete source-bound sample')
        staged=[]
        for sample in samples:
            metadata=self.jobs.media.put(pid,[sample.png],'image/png','worker_service',
                derivative_of=source.model_dump(),publish=lambda value:value)
            timing=asdict(sample)
            timing.pop('png')
            staged.append((metadata,timing))
        with self.jobs.store.transaction() as db:
            current=self.jobs.store.get_object(pid,job['object_id'],conn=db)
            current=self.jobs._fenced(self.principal,pid,_ref(current),fence,db)
            self.jobs.submissions.revalidate(pid,_ref(current),conn=db)
            frames=[]
            for metadata,timing in staged:
                media=self.jobs.store.create_object(pid,'media',{**metadata,'frame_evidence':timing,
                    'dependencies':[source.model_dump()]},'worker_service',conn=db)
                frames.append({**timing,'media':_ref(media).model_dump()})
            pack={'source':source.model_dump(),'source_sha256':intent['body']['request']['media']['sha256'],
                  'release_id':intent['body']['release_id'],'policy_hash':content_hash(policy),'frames':frames,
                  'sampled':True,'exhaustive':False,'replaces_original_video':False}
            return self.jobs._write(pid,current,{'prepared_frames':pack},'observation.frames_prepared',db)

    def _preparation_failed(self, pid: str, job: dict[str, Any], fence: int, code: str) -> dict[str, Any]:
        """Release only provably uncalled work; never alter another lease owner."""
        with self.jobs.store.transaction() as db:
            current=self.jobs.store.get_object(pid,job['object_id'],conn=db)
            current=self.jobs._fenced(self.principal,pid,_ref(current),fence,db)
            if current['body']['state']!='queued' or current['body'].get('attempt_id') or current['body'].get('remote_job_id'):
                raise DomainError('forbidden','Only never-dispatched preparation may release its reservation')
            self.jobs._settle(pid,current,0,db)
            return self.jobs._write(pid,current,{'state':'failed','lease':None,
                'preparation_failure':{'code':code,'provider_called':False}},'observation.preparation_failed',db)

    def _record_observation(self, pid: str, job: dict[str, Any], fence: int, intent: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        with self.jobs.store.transaction() as db:
            current=self.jobs.store.get_object(pid,job['object_id'],conn=db)
            current=self.jobs._fenced(self.principal,pid,_ref(current),fence,db)
            source=intent['body']['request']['media']
            if (current['body']['state']!='dispatching' or current['body']['intent']!=_ref(intent).model_dump()
                    or result['status'] not in ('succeeded','failed','unknown') or result['source']!=source['object_ref']
                    or result['source_sha256']!=source['sha256'] or result['intent']!=_ref(intent).model_dump()):
                raise DomainError('forbidden','Observation result does not bind this dispatched original')
            routes=self.jobs.submissions.workflow.config.section('review_routes')
            policy=self._frame_policy(routes['profiles'][intent['body']['route']['profile_id']])
            frames=self._check_frames(pid,current['body'].get('prepared_frames',{}),intent,policy,db) if policy else []
            # Only service preparation supplies frames; a reader cannot claim to
            # have provided images or replace the original video/audio evidence.
            result={**result,'frames':frames,'frame_evidence':{'sampled':bool(frames),'exhaustive':False,
                'replaces_original_video':False,'policy_hash':content_hash(policy) if policy else None},
                'dependencies':[*result['dependencies'], *[frame['media'] for frame in frames]]}
            observation=self.jobs.store.create_object(pid,'observation',result,'reader_service',conn=db)
            self.jobs._settle(pid,current,None,db)
            return self.jobs._write(pid,current,{'state':result['status'],'result':_ref(observation).model_dump(),
                'observation':_ref(observation).model_dump(),'lease':None},'observation.recorded',db)

    def _process_aux(self, pid: str, job: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        fence=None
        dispatched=False
        preparing=False
        body=intent['body']
        local=body['operation']=='render-cut'
        try:
            if local:
                if self.cuts is None or body['cost']['mode']!='local':
                    raise DomainError('unsupported_route','Local cut handler is not configured')
                self.cuts.policy()
                if job['body']['state'] in ('dispatching','unknown'):
                    job=self._local_retry(pid,job,intent)
                    if job['body']['state']!='queued':
                        return self._outcome(job,'local-recovery')
            else:
                if self.reader is None:
                    raise DomainError('unsupported_route','Observation handler is not configured')
                routes=self.reader.config.section('review_routes')
                profile=routes['profiles'][body['route']['profile_id']]
                self.reader._settings(body,profile)
                if self.jobs.lease_seconds<=profile['limits']['request_timeout_seconds']+5:
                    raise DomainError('invalid_input','Observation lease is shorter than its released request deadline')
            claim=self.jobs.claim(self.principal,pid,job['object_id'])
            job,fence=claim['job'],claim['fence']
            if claim['action']=='quarantined':
                return self._outcome(job,'quarantined')
            if claim['action']!='dispatch' or fence is None:
                raise DomainError('unsupported_route','Auxiliary tasks do not support remote polling')
            if not local:
                preparing=True
                job=self._derivative(pid,job,fence,intent,profile)
                job=self._prepare_frames(pid,job,fence,intent,profile)
                preparing=False
            dispatch=self.jobs.begin_dispatch(self.principal,pid,_ref(job),fence)
            job=dispatch['job']
            dispatched=True
            if local:
                assert self.cuts is not None
                self.cuts.render(self.principal,pid,ObjectRef.model_validate(body['target']),jobs=self.jobs,job_ref=_ref(job),fence=fence)
                job=self.jobs.store.get_object(pid,job['object_id'])
            else:
                assert self.reader is not None
                derivative=job['body'].get('visual_derivative')
                result=self.reader.observe(self.principal,pid,_ref(job),fence,
                    visual_derivative=ObjectRef.model_validate(derivative) if derivative else None)
                job=self._record_observation(pid,job,fence,dispatch['intent'],result)
            return self._outcome(job,'render-cut' if local else 'observe')
        except Exception as exc:  # noqa: BLE001 -- paid ambiguity must remain durable, local work may retry.
            code=exc.code if isinstance(exc,DomainError) else 'provider_failure'
            if preparing and not dispatched and fence is not None:
                try:
                    job=self._preparation_failed(pid,job,fence,code)
                    return self._outcome(job,'observe-preparation',code)
                except DomainError:
                    pass  # Lost ownership cannot release budget or publish evidence.
            if local and dispatched and fence is not None:
                try:
                    current=self._owned(pid,job['object_id'],fence)
                    return self._outcome(self._local_retry(pid,current,intent,fence=fence),'local-retry',code)
                except DomainError:
                    pass
            return self._error(pid,job,fence,'render-cut' if local else 'observe',code,dispatched)

    def _process(self, pid: str, job: dict[str, Any]) -> dict[str, Any]:
        fence=None
        action='blocked'
        after_dispatch=False
        try:
            intent=self._intent(pid,job)
            body=intent['body']
            # A draft completion is a paid provider job like a take: sent, polled and downloaded the same way.
            if body['operation'] not in ('submit','complete-draft'):
                return self._process_aux(pid,job,intent)
            if self.provider is None:
                raise DomainError('unsupported_route','Generation adapter is unconfigured')
            mode=body['cost']['mode']
            if mode not in ('fake','live') or (mode=='fake') != self.provider.fake:
                raise DomainError('unsupported_route','Job execution mode differs from provider adapter')
            if self.provider.fake and self.jobs.downloader.transport is _http:
                raise DomainError('unsupported_route','Fake generation requires an injected non-network result transport')
            if job['body']['state']=='queued' and not self.provider.fake and not self.provider.live_enabled:
                raise DomainError('unsupported_route','Live create requires explicit service enablement')
            claim=self.jobs.claim(self.principal,pid,job['object_id'])
            job,fence,action=claim['job'],claim['fence'],claim['action']
            if action=='quarantined':
                return self._outcome(job,action)
            assert fence is not None
            request=body['request']
            if action=='dispatch':
                refs=self._references(pid,request)
                # Local validation only; no version/cost/create subprocess here.
                self.provider._parameters(request,has_references=bool(refs))
                self.provider._references(request,refs)
                self.provider._isolation()
                dispatch=self.jobs.begin_dispatch(self.principal,pid,_ref(job),fence)
                job=dispatch['job']
                after_dispatch=True
                receipt=self.provider.submit(dispatch['intent']['body'],refs)
            elif action=='poll':
                after_dispatch=True
                receipt=self.provider.get(job['body']['remote_job_id'],request)
            elif action=='download':
                after_dispatch=True
                return self._outcome(self.jobs.download_result(self.principal,pid,_ref(job),fence),action)
            else:
                raise DomainError('unsupported_route','Worker claim action is unsupported')
            job=self._owned(pid,job['object_id'],fence)
            job=self.jobs.record_receipt(self.principal,pid,_ref(job),fence,receipt)
            if job['body'].get('pending_result'):
                job=self.jobs.download_result(self.principal,pid,_ref(job),fence)
            return self._outcome(job,action)
        except DomainError as exc:
            return self._error(pid,job,fence,action,exc.code,after_dispatch)
        except Exception:  # noqa: BLE001 -- unexpected post-effect failures must leave durable uncertainty.
            return self._error(pid,job,fence,action,'provider_failure',after_dispatch)

    def _advance_films(self) -> None:
        """At most one film stage per project per interval; a failure is logged and kept visible, never hidden."""
        if self.film is None:
            return
        now = time.monotonic()
        for pid in self.projects:
            if self._film_due.get(pid, 0.0) > now:
                continue
            self._film_due[pid] = now + self.film_interval
            try:
                state = self.film.advance(pid)
            except DomainError as exc:
                state = 'blocked:' + exc.code
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
                state = 'blocked:' + type(exc).__name__
            if self.film_status.get(pid) != state:
                print(f'auto-film {pid} {state}', file=sys.stderr, flush=True)
            self.film_status[pid] = state

    def _advance_completions(self) -> None:
        """order the 1080p completion of picks older than ten minutes."""
        if self.complete is None:
            return
        now = time.monotonic()
        for pid in self.projects:
            if self._complete_due.get(pid, 0.0) > now:
                continue
            self._complete_due[pid] = now + self.film_interval
            try:
                state = self.complete.advance(pid)
            except DomainError as exc:
                state = 'blocked:' + exc.code
            except (OSError, ValueError, KeyError, TypeError) as exc:
                state = 'blocked:' + type(exc).__name__
            if self.complete_status.get(pid) != state:
                print(f'auto-complete {pid} {state}', file=sys.stderr, flush=True)
            self.complete_status[pid] = state

    def _advance_shoots(self) -> None:
        """one shoot step per project per cycle while an order is open."""
        if self.shoot is None:
            return
        now = time.monotonic()
        for pid in self.projects:
            if self._shoot_due.get(pid, 0.0) > now:
                continue
            try:
                state = self.shoot.advance(pid)
            except DomainError as exc:
                state = 'blocked:' + exc.code
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
                state = 'blocked:' + type(exc).__name__
            # Idle: look again later; with open cards, every couple of seconds (each call is one quick step per card).
            self._shoot_due[pid] = now + (self.film_interval if state == 'idle' else 2.0)
            if self.shoot_status.get(pid) != state:
                print(f'auto-shoot {pid} {state}', file=sys.stderr, flush=True)
            self.shoot_status[pid] = state

    LISTING_RETRY_SECONDS = 30.0
    LISTING_OPERATOR_SECONDS = 600.0

    def _listing_due(self, job: dict[str, Any], intent: dict[str, Any]) -> bool:
        """A timed-out create the worker may settle from the provider listing itself."""
        body=intent['body']
        return (self.provider is not None and body.get('operation')=='submit'
                and getattr(self.provider,'can_list',lambda job_type: True)((body.get('request') or {}).get('job_type'))
                and body.get('cost',{}).get('mode')==('fake' if self.provider.fake else 'live')
                and time.monotonic()>=self._listing_next.get(job['object_id'],0.0))

    def _settle_listed(self, pid: str, job: dict[str, Any]) -> dict[str, Any]:
        """Adopt the listed create or close it as absent; otherwise look again shortly. Never resubmits.

        Owner 2026-09-24: the answer is in the listing as soon as the attempt window closes, so no
        operator has to run a command and no one waits thirty minutes.
        """
        assert self.provider is not None
        oid=job['object_id']
        self._listing_next[oid]=time.monotonic()+self.LISTING_RETRY_SECONDS
        mode='fake' if self.provider.fake else 'live'
        store=self.jobs.store
        try:
            with store.transaction(write=False) as db:
                seen=listing_reconcile.inspect(store,pid,oid,db,cost_mode=mode)
            captured=listing_reconcile.now().isoformat()
            listed=self.provider.list_recent(seen['job_type'])
            with store.transaction() as db:
                seen=listing_reconcile.inspect(store,pid,oid,db,cost_mode=mode)
                taken=listing_reconcile.owned(store,db,except_job=oid)
                decision,value=listing_reconcile.decide(seen,listed,captured,taken,listing_reconcile.rivals(store,pid,seen,db))
                if decision=='operator':
                    self._listing_next[oid]=time.monotonic()+self.LISTING_OPERATOR_SECONDS
                    print(f'listing {pid} {oid} needs the operator: {value}',file=sys.stderr,flush=True)
                if decision not in ('adopt','absent'):
                    return {'job_id':oid,'action':'listing-'+decision,'state':'unknown'}
                remote=value if decision=='adopt' else None
                result=listing_reconcile.apply(store,pid,seen,remote_job_id=remote,
                    unowned=listing_reconcile.matches(seen,listed)-taken,evidence_sha256=content_hash(listed),
                    reason=('The listing shows one unowned exact match, adopted so the worker polls it.' if remote else
                            'The listing captured after the attempt window holds no unowned exact match, '
                            'so the create never reached the provider.'),
                    author='worker_service',db=db,listing={'captured_at':captured,'jobs':listed})
        except DomainError as exc:
            return {'job_id':oid,'action':'listing','state':'unknown','error_code':exc.code}
        except Exception:  # noqa: BLE001 -- a transient store or CLI failure retries later, never stops the loop.
            return {'job_id':oid,'action':'listing','state':'unknown','error_code':'provider_failure'}
        self._listing_next.pop(oid,None)
        return {'job_id':oid,'action':'listing-'+decision,'state':result['state'],'remote_job_id':remote}

    def run_once(self) -> list[dict[str, Any]]:
        """All wired task kinds share slots. Unsupported/unknown paid work never runs."""
        review_http.assert_healthy()
        self._refresh_projects()
        self._advance_completions()
        self._advance_films()
        self._advance_shoots()
        pending=[]
        settle: set[str] = set()
        creates_paid: set[str] = set()  # queued Higgsfield creates, the only uploads
        lane=-1
        with self.jobs.store.transaction(write=False) as db:
            for pid in self.projects:
                self.jobs.auth.authorize(self.principal,pid,'dispatch',conn=db)
                lane+=1
                for job in self.jobs.store.list_objects(pid,kind='job',conn=db):
                    body=job['body']
                    if body['state'] in ('succeeded','failed','cancelled') or (body.get('lease') or {}).get('expires_at',0)>self.jobs.clock():
                        continue
                    if body['state']=='unknown' and (
                            body.get('pending_result') and body.get('download_count',0)>=self.jobs.max_downloads
                            or body.get('remote_job_id') and not body.get('pending_result')
                            and body.get('poll_count',0)>=self.jobs.max_polls):
                        continue
                    intent=self._intent(pid,job,conn=db)
                    local=intent['body']['operation']=='render-cut' and intent['body']['cost']['mode']=='local'
                    if body['state']=='unknown' and not local and not body.get('remote_job_id') and not body.get('pending_result'):
                        if not self._listing_due(job,intent):
                            continue
                        settle.add(job['object_id'])
                        pending.append(((lane,job['object_id']),pid,job))
                        continue
                    if body['state'] in ('submitted','running','unknown') and not body.get('pending_result') and body.get('next_poll_at',0)>self.jobs.clock():
                        continue
                    if intent['body']['operation'] not in self.operations:
                        continue
                    if body['state']=='queued' and intent['body']['operation'] in ('submit','complete-draft'):
                        creates_paid.add(job['object_id'])
                    pending.append(((lane,job['object_id']),pid,job))
        # A busy first project/kind (including repeated remote polls) must not
        # prevent other authorized work from being considered. This cursor is
        # only scheduling preference; durable leases still own every execution.
        pending.sort(key=lambda item:(item[0]<=self._scan_after,item[0]))
        selected=[]
        creates=0
        for key,pid,job in pending:
            if len(selected)>=self.concurrency:
                break
            # Uploads to Higgsfield time out when too many run at once; director reviews and
            # other work may still fill the remaining slots (owner 2026-09-24: watch takes together).
            create=job['object_id'] in creates_paid
            if create and creates>=self.create_concurrency:
                continue
            if self._slots.acquire(blocking=False):
                selected.append((pid,job))
                creates+=create
                self._scan_after=key
        if not selected:
            return []
        def work(item: tuple[str,dict[str, Any]]) -> dict[str, Any]:
            try:
                if item[1]['object_id'] in settle:
                    return self._settle_listed(*item)
                return self._process(*item)
            finally:
                self._slots.release()
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            return list(pool.map(work,selected))

    def run(self, stop_event: threading.Event, *, max_iterations: int | None = None, poll_interval: float = 1) -> list[dict[str, Any]]:
        """Interruptible persistent loop; bounded runs return outcomes for operators/tests.

        Persistent mode does not accumulate history in RAM; SQLite is the journal.
        """
        if max_iterations is not None and (type(max_iterations) is not int or not 0<=max_iterations<=100000):
            raise ValueError('Invalid iteration bound')
        if not 0.01<=poll_interval<=60:
            raise ValueError('Polling interval must be bounded')
        outcomes=[]
        iteration=0
        while not stop_event.is_set() and (max_iterations is None or iteration<max_iterations):
            current=self.run_once()
            if max_iterations is not None:
                outcomes.extend(current)
            iteration+=1
            if max_iterations is None or iteration<max_iterations:
                stop_event.wait(poll_interval)
        return outcomes


class HFConfiguration(Contract):
    native_path: str
    sha256: Annotated[str,Field(pattern=r'^[0-9a-f]{64}$')]
    version: str
    credential_home: str
    service_uid: Annotated[int,Field(ge=0)]
    capability_roles: dict[str,str]
    timeout: Annotated[int,Field(ge=1,le=120)] = 60


class ApilioImagesConfiguration(Contract):
    # job type -> released capability role (models and pixel sizes); the key stays in the env.
    capability_roles: dict[str,str]
    timeout: Annotated[int,Field(ge=10,le=600)] = 180


class FalConfiguration(Contract):
    # job type (fal_seedance_2_5, fal_seedance_2_5_complete) -> released capability role;
    # FAL_AI_TOKEN stays in the env. The timeout bounds one queue call, not the render (the worker polls).
    capability_roles: dict[str,str]
    timeout: Annotated[int,Field(ge=10,le=600)] = 120


class WorkerConfiguration(Contract):
    storage: StorageConfiguration
    public_origin: str
    # Empty = follow the worker credential's scope; a list pins it (rehearsal copies).
    project_ids: Annotated[list[str],Field(max_length=1000)] = Field(default_factory=list)
    worker_token_env: Annotated[str,Field(pattern=r'^[A-Z][A-Z0-9_]{1,127}$')]
    hf: HFConfiguration | None = None
    apilio_images: ApilioImagesConfiguration | None = None
    fal: FalConfiguration | None = None
    download_hosts: list[str] = Field(default_factory=list)
    reader_enabled: bool = False
    cuts_enabled: bool = False
    live_enabled: Annotated[bool,Field(strict=True)] = False
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    concurrency: Annotated[int,Field(ge=1,le=12)] = 1
    create_concurrency: Annotated[int,Field(ge=1,le=12)] | None = None
    lease_seconds: Annotated[int,Field(ge=1,le=1000)] = 300
    poll_interval: Annotated[float,Field(ge=0.01,le=60)] = 1.0
    # Agent credential of the film service; without it picks never become a film by themselves.
    film_token_env: Annotated[str,Field(pattern=r'^[A-Z][A-Z0-9_]{1,127}$')] | None = None
    # the highest event number when this worker was deployed. Only 再拍一批 answers after it
    # re-fire by themselves; not set (no default is assumed) means no automatic reshoot at all.
    reshoot_after_event: Annotated[int,Field(ge=0,strict=True)] | None = None


def _hf_prices(settings: RuntimeConfig, capabilities: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """the pinned Higgsfield credits per second, by job type and resolution."""
    try:
        price = settings.section('hf_video_price')
    except DomainError:
        return {}
    return {price['job_type']: dict(price['credits_per_second'])} if price.get('job_type') in capabilities else {}


def _absolute(value: str) -> Path:
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or path.is_symlink():
        raise DomainError('forbidden','Service paths must be explicit absolute paths without symlink targets')
    return path


def build_worker(config_path: Path) -> Worker:
    """Named service bootstrap. Config is private, service-owned, strict JSON.

    Required keys are WorkerConfiguration's schema. Worker authentication comes
    from worker_token_env; provider credentials are loaded lazily only for the
    fixed released review routes when live execution is explicitly enabled.
    HF OAuth is isolated in its explicit private HOME.
    """
    try:
        fd=os.open(config_path,os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077
                    or info.st_size>65536):
                raise DomainError('forbidden','Worker configuration must be a bounded private service-owned file')
            raw=stream.read(65537)
            if len(raw)>65536:
                raise DomainError('forbidden','Worker configuration is too large')
        config=WorkerConfiguration.model_validate_json(raw)
    except (OSError,ValueError,ValidationError) as exc:
        raise DomainError('invalid_input','Worker configuration is missing, unsafe or does not match its schema') from exc
    if config.hf is not None and config.hf.service_uid!=os.geteuid():
        raise DomainError('forbidden','Configured worker identity differs from the running service UID')
    secret=os.environ.get(config.worker_token_env)
    if not secret:
        raise DomainError('unauthorized','Configured worker credential environment variable is absent')
    runtime=open_storage(config.storage,role='worker')
    try:
        return _build_worker_services(config,runtime,secret)
    except BaseException:
        runtime.close()
        raise


def _build_worker_services(config: WorkerConfiguration, runtime: RuntimeStorage, secret: str) -> Worker:
    store,media=runtime.store,runtime.media
    auth=AuthService(store,config.public_origin)
    principal=auth.authenticate(secret)
    settings=RuntimeConfig.load()
    workflow=Workflow(store,auth,settings)
    gates=Gates(store,workflow,media)
    # no AI review lane. `review_enabled` is still an
    # accepted key of the live config and is ignored; the reader (Gemini observe) stays.
    submissions=Submissions(store,auth,workflow,gates,live_enabled=config.live_enabled)
    jobs=Jobs(store,auth,submissions,media,download_hosts=set(config.download_hosts),lease_seconds=config.lease_seconds)
    # one adapter per job type behind a router, so a job is only ever sent, polled
    # or settled by the provider that made it.
    adapters: dict[str,Any]={}
    if config.hf is not None:
        native,home=_absolute(config.hf.native_path),_absolute(config.hf.credential_home)
        capabilities={model:settings.section(role) for model,role in config.hf.capability_roles.items()}
        hf=HFProvider(capabilities,ExecutablePin(native,config.hf.sha256,config.hf.version),
            service_home=home,service_uid=config.hf.service_uid,media_root=media.root,timeout=config.hf.timeout,
            live_enabled=config.live_enabled,credits_per_second=_hf_prices(settings,capabilities))
        hf._isolation()
        adapters.update(dict.fromkeys(capabilities,hf))
    if config.apilio_images is not None:
        capabilities={job:settings.section(role) for job,role in config.apilio_images.capability_roles.items()}
        adapters.update(dict.fromkeys(capabilities,ApilioImages(capabilities,live_enabled=config.live_enabled,
                                                                 timeout=config.apilio_images.timeout)))
    if config.fal is not None:
        capabilities={job:settings.section(role) for job,role in config.fal.capability_roles.items()}
        adapters.update(dict.fromkeys(capabilities,FalSeedance(capabilities,live_enabled=config.live_enabled,timeout=config.fal.timeout)))
    provider=ProviderRouter(adapters) if adapters else None
    ffmpeg=_absolute(config.ffmpeg_path) if config.ffmpeg_path else None
    ffprobe=_absolute(config.ffprobe_path) if config.ffprobe_path else None
    if (config.reader_enabled or config.cuts_enabled) and (ffmpeg is None or not ffmpeg.is_file()):
        raise DomainError('missing_prerequisite','Auxiliary workers require an explicit service FFmpeg executable')
    reader=Reader(jobs,settings) if config.reader_enabled else None
    cuts=Cuts(store,auth,workflow,media,ffmpeg=ffmpeg) if config.cuts_enabled else None
    if reader:
        settings.section('review_routes')
    if cuts:
        cuts.policy()
    film=None
    shoot=None
    if config.film_token_env is not None:
        film_secret=os.environ.get(config.film_token_env)
        if not film_secret or cuts is None:
            raise DomainError('missing_prerequisite','The film service needs its credential and cuts')
        # The owner's confirmation is the acceptance; no review receipts are required.
        decisions=Decisions(store,auth,workflow,cuts,ttl_seconds=86400,human_confirmation_enabled=True)
        film_principal=auth.authenticate(film_secret)
        film=AutoFilm(film_principal,cuts,submissions,decisions)
        # The same service identity finishes shoot orders: fire when allowed, listen, offer on the desk.
        batches=Batches(store,auth,workflow,submissions,Reviews(store,auth,workflow,media))
        shoot=AutoShoot(film_principal,store,Shoot(store,auth,None,None,batches),decisions,media,
                        reshoot_after_event=config.reshoot_after_event)
    worker=Worker(jobs,provider,principal,config.project_ids,concurrency=config.concurrency,create_concurrency=config.create_concurrency,reader=reader,cuts=cuts,ffmpeg=ffmpeg,ffprobe=ffprobe,
                  film=film,shoot=shoot,
                  complete=AutoComplete(principal,submissions) if config.fal is not None else None)
    worker.poll_interval=config.poll_interval
    worker.runtime_storage=runtime
    return worker


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description='Run the isolated MVGP production worker; unverified live routes remain disabled.')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(argv)
    worker=None
    try:
        worker=build_worker(args.config)
        print(json.dumps(worker.configuration_status))
        if args.once:
            print(json.dumps(worker.run_once()))
        else:
            stop=threading.Event()
            # a stop (deploy, rollback, restart) lets the send in flight finish and record its
            # receipt; only then does the loop end. Killing it mid-send would lose a paid request's id.
            previous=signal.signal(signal.SIGTERM,lambda *_: stop.set())
            try:
                worker.run(stop,poll_interval=getattr(worker,'poll_interval',1.0))
            except KeyboardInterrupt:
                stop.set()
            finally:
                signal.signal(signal.SIGTERM,previous)
        return 0
    except (DomainError,OSError,ValueError) as exc:
        print(json.dumps({'error_code':exc.code if isinstance(exc,DomainError) else 'invalid_input'}))
        return 1
    finally:
        if worker is not None:
            worker.close()


if __name__=='__main__':
    raise SystemExit(main())
