"""Durable fenced jobs. Network effects occur only after dispatching commits.

Lease expiry after dispatch never authorizes another create. Provider receipts
precede downloads. Credential-bearing result URLs live only in the inaccessible
system partition; project receipts contain an opaque private reference.
"""
from __future__ import annotations

import http.client
import ipaddress
import queue
import socket
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from production.auth import SYSTEM_PROJECT, AuthService, Principal
from production.contracts import DomainError, ObjectRef, content_hash
from production.media import MediaStore
from production.output_contract import check_result, validate_policy, validate_request
from production.patches import link_generated_repairs
from production.provider_types import ProviderReceipt
from production.store import Store
from production.submissions import Submissions

SERVICE_AUTHOR = 'worker_service'


@dataclass(frozen=True)
class Download:
    media_type: str
    chunks: Iterable[bytes]


def _resolve(host: str) -> list[str]:
    return list(dict.fromkeys(str(row[4][0]) for row in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)))


@contextmanager
def _http(url: str, host: str, ip: str, timeout: float) -> Iterator[Download]:
    # Connect to the validated address, while TLS and Host retain the hostname.
    parsed = urlsplit(url)
    connection = http.client.HTTPSConnection(host,443,timeout=timeout,context=ssl.create_default_context())
    started = time.monotonic()
    raw = socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET,socket.SOCK_STREAM)
    raw.settimeout(timeout)
    sockets=[raw]
    def abort() -> None:
        for active in sockets:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active.close()
    deadline=threading.Timer(timeout,abort)
    deadline.daemon=True
    deadline.start()
    try:
        raw.connect((ip,443))
        secured = ssl.create_default_context().wrap_socket(raw,server_hostname=host,do_handshake_on_connect=False)
        sockets.append(secured)
        secured.do_handshake()
        connection.sock = secured
        connection.request('GET',(parsed.path or '/')+('?' + parsed.query if parsed.query else ''),headers={'Accept-Encoding':'identity'})
        response = connection.getresponse()
        if response.status != 200 or response.getheader('Content-Encoding','identity') != 'identity':
            raise DomainError('provider_failure','Result download refuses redirects, errors and encoded bodies')
        def chunks() -> Iterator[bytes]:
            while True:
                remaining=timeout-(time.monotonic()-started)
                if remaining<=0:
                    raise DomainError('provider_failure','Result download deadline exceeded')
                secured.settimeout(remaining)
                data=response.read1(65536)
                if not data:
                    break
                yield data
        yield Download(response.getheader('Content-Type','').split(';')[0].lower(),chunks())
    finally:
        deadline.cancel()
        connection.close()
        raw.close()


class SafeDownloader:
    def __init__(self, hosts: set[str], *, resolver: Callable[[str], list[str]] = _resolve,
                 transport: Callable[[str,str,str,float], AbstractContextManager[Download]] = _http) -> None:
        self.hosts,self.resolver,self.transport = frozenset(h.lower() for h in hosts),resolver,transport
        self._dns_slots = threading.BoundedSemaphore(4)

    def _addresses(self, host: str, timeout: float) -> list[str]:
        if not self._dns_slots.acquire(blocking=False):
            raise DomainError('insufficient_context','Bounded DNS worker capacity is occupied')
        results: queue.Queue[Any] = queue.Queue(maxsize=1)
        def resolve() -> None:
            try:
                results.put(self.resolver(host))
            except (OSError, ValueError, TypeError):
                results.put(None)
            finally:
                self._dns_slots.release()
        threading.Thread(target=resolve,daemon=True).start()
        try:
            addresses=results.get(timeout=timeout)
        except queue.Empty as exc:
            raise DomainError('provider_failure','Result DNS deadline exceeded') from exc
        if not isinstance(addresses,list):
            raise DomainError('provider_failure','Result DNS lookup failed')
        return addresses

    @contextmanager
    def open(self, url: str, *, max_bytes: int, timeout: float) -> Iterator[Download]:
        try:
            parsed=urlsplit(url)
            host=parsed.hostname or ''
            if (parsed.scheme!='https' or host not in self.hosts or parsed.username or parsed.password
                    or parsed.port not in (None,443) or parsed.fragment or any(ord(c)<32 for c in url)):
                raise DomainError('forbidden','Result URL is outside the exact HTTPS host allowlist')
            started=time.monotonic()
            addresses=self._addresses(host,timeout)
            if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
                raise DomainError('forbidden','Result hostname resolves to a non-public address')
            remaining=timeout-(time.monotonic()-started)
            if remaining<=0:
                raise DomainError('provider_failure','Result DNS deadline exceeded')
            with self.transport(url,host,addresses[0],remaining) as response:
                def bounded() -> Iterator[bytes]:
                    size=0
                    for chunk in response.chunks:
                        if not isinstance(chunk,bytes):
                            raise DomainError('invalid_media','Download transport returned non-byte content')
                        size+=len(chunk)
                        if size>max_bytes or time.monotonic()-started>timeout:
                            raise DomainError('invalid_media','Result download exceeded its byte or time limit')
                        yield chunk
                yield Download(response.media_type,bounded())
        except (OSError,ValueError,http.client.HTTPException) as exc:
            raise DomainError('provider_failure','Result download failed safely') from exc


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k:obj[k] for k in ('object_id','revision','digest')}


# The worker stops polling a job after this many status reads (a live worker keeps the default).
MAX_POLLS = 20

class Jobs:
    def __init__(self, store: Store, auth: AuthService, submissions: Submissions, media: MediaStore, *,
                 download_hosts: set[str] | None = None, downloader: SafeDownloader | None = None,
                 clock: Callable[[],float] = time.time, lease_seconds: int = 300, max_polls: int = MAX_POLLS,
                 poll_delay: int = 2, max_downloads: int = 3, download_timeout: int = 120) -> None:
        if any(type(v) is not int or not 1<=v<=1000 for v in (lease_seconds,max_polls,poll_delay,max_downloads,download_timeout)):
            raise ValueError('Worker lease/retry/time limits must be bounded positive integers')
        self.store,self.auth,self.submissions,self.media=store,auth,submissions,media
        self.downloader=downloader or SafeDownloader(download_hosts or set())
        self.clock,self.lease_seconds,self.max_polls,self.poll_delay=clock,lease_seconds,max_polls,poll_delay
        self.max_downloads,self.download_timeout=max_downloads,download_timeout

    def _job(self, worker: Principal, pid: str, ref: ObjectRef | str, db: sqlite3.Connection,
             operation: str = 'record-provider-result') -> dict[str, Any]:
        self.auth.authorize(worker,pid,operation,conn=db)
        obj=self.store.get_object(pid,ref if isinstance(ref,str) else ref.object_id,conn=db)
        if obj['kind']!='job' or obj['author'] not in ('submission_service',SERVICE_AUTHOR):
            raise DomainError('forbidden','Expected a service-owned job')
        if isinstance(ref,ObjectRef) and (obj['revision']!=ref.revision or ref.digest and obj['digest']!=ref.digest):
            raise DomainError('revision_conflict','Job changed',current_revision=obj['revision'])
        return obj

    def _fenced(self, worker: Principal, pid: str, ref: ObjectRef, fence: int, db: sqlite3.Connection) -> dict[str, Any]:
        job=self._job(worker,pid,ref,db)
        lease=job['body'].get('lease') or {}
        if (type(fence) is not int or lease.get('fence')!=fence or lease.get('credential_id')!=worker.credential_id
                or lease.get('expires_at',0)<=self.clock()):
            raise DomainError('revision_conflict','Worker lease is stale or belongs to another worker')
        return job

    def _write(self, pid: str, job: dict[str, Any], changes: dict[str, Any], event: str, db: sqlite3.Connection) -> dict[str, Any]:
        result=self.store.append_revision(pid,job['object_id'],job['revision'],{**job['body'],**changes},SERVICE_AUTHOR,conn=db)
        self.store.append_event(pid,event,{'job':_ref(result),'state':result['body']['state']},conn=db)
        return result

    def _settle(self, pid: str, job: dict[str, Any], amount: int | None, db: sqlite3.Connection) -> None:
        reservation=job['body'].get('reservation_id')
        if reservation:
            row=db.execute('SELECT state FROM reservations WHERE project_id=? AND reservation_id=?',(pid,reservation)).fetchone()
            if amount is None and row and row['state']=='settled':
                return
            self.store.settle(pid,reservation,amount,conn=db)

    def claim(self, worker: Principal, project_id: str, job_id: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            job=self._job(worker,project_id,job_id,db,'dispatch')
            body=job['body']
            already_unknown=body['state']=='unknown'
            if body['state'] in ('succeeded','failed','cancelled'):
                raise DomainError('revision_conflict','Job is terminal')
            if (body.get('lease') or {}).get('expires_at',0)>self.clock():
                raise DomainError('revision_conflict','Job already has an active worker lease')
            if body['state']=='dispatching':
                self._settle(project_id,job,None,db)
                job=self._write(project_id,job,{'state':'unknown','lease':None},'job.expired_dispatch',db)
                body=job['body']
            action='download' if body.get('pending_result') else 'dispatch' if body['state']=='queued' else 'poll' if body.get('remote_job_id') else 'quarantined'
            count_key='download_count' if action=='download' else 'poll_count'
            limit=self.max_downloads if action=='download' else self.max_polls
            if action=='quarantined' or action in ('poll','download') and body.get(count_key,0)>=limit:
                if already_unknown:
                    raise DomainError('revision_conflict','Job is already quarantined')
                job=self._write(project_id,job,{'state':'unknown','lease':None},'job.quarantined',db)
                return {'job':job,'fence':None,'action':'quarantined'}
            if action=='poll' and body.get('next_poll_at',0)>self.clock():
                raise DomainError('revision_conflict','Polling backoff has not elapsed')
            fence=body.get('fence_counter',0)+1
            changes={'lease':{'credential_id':worker.credential_id,'fence':fence,'expires_at':self.clock()+self.lease_seconds},
                     'fence_counter':fence}
            if action=='poll':
                changes[count_key]=body.get(count_key,0)+1
            job=self._write(project_id,job,changes,'job.claimed',db)
            return {'job':job,'fence':fence,'action':action}

    # Reasons the frozen request can never be sent again (bug hunt 2026-09-25): the card or its inputs changed, the
    # release changed, the maker's credential went, or a fal draft passed its seven days. A queued job refused for one
    # of these is cancelled and its hold released at zero, since nothing was sent; any other refusal retries.
    NEVER_SENDABLE = frozenset({'stale_input', 'rule_violation', 'release_mismatch', 'review_required', 'unauthorized', 'forbidden'})

    def begin_dispatch(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int) -> dict[str, Any]:
        try:
            return self._begin_dispatch(worker, project_id, job_ref, fence)
        except DomainError as exc:
            if exc.code in self.NEVER_SENDABLE:
                self._cancel_unsent(worker, project_id, job_ref, fence, exc.code)
            raise

    def _cancel_unsent(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int, code: str) -> None:
        try:
            with self.store.transaction() as db:
                job=self._fenced(worker,project_id,job_ref,fence,db)
                if job['body']['state']!='queued' or job['body'].get('attempt_id'):
                    return
                self._settle(project_id,job,0,db)
                self._write(project_id,job,{'state':'cancelled','lease':None,'last_error':{'code':code,
                    'reason':'The request is no longer valid and was never sent; its hold is released.'}},'job.cancelled_unsent',db)
        except DomainError:
            pass  # a lost lease or a job already moved on: the next claim decides again

    def _begin_dispatch(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int) -> dict[str, Any]:
        with self.store.transaction() as db:
            job=self._fenced(worker,project_id,job_ref,fence,db)
            intent=self.submissions.revalidate(project_id,ObjectRef(**_ref(job)),conn=db)
            attempt=self.store.create_object(project_id,'provider-attempt',{'intent':_ref(intent),'job_id':job['object_id'],
                'fence':fence,'request':intent['body']['request'],'dependencies':[_ref(intent)]},SERVICE_AUTHOR,conn=db)
            lease={**job['body']['lease'],'expires_at':self.clock()+self.lease_seconds}
            job=self._write(project_id,job,{'state':'dispatching','attempt_id':attempt['object_id'],'lease':lease},'job.dispatching',db)
            return {'job':job,'fence':fence,'intent':intent,'attempt':attempt}

    def renew(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int) -> dict[str, Any]:
        with self.store.transaction() as db:
            job=self._fenced(worker,project_id,job_ref,fence,db)
            return self._write(project_id,job,{'lease':{**job['body']['lease'],'expires_at':self.clock()+self.lease_seconds}},'job.lease_renewed',db)

    def record_receipt(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int,
                       receipt: ProviderReceipt) -> dict[str, Any]:
        with self.store.transaction() as db:
            job=self._fenced(worker,project_id,job_ref,fence,db)
            body=job['body']
            intent=self.store.get_object(project_id,body['intent']['object_id'],conn=db)
            if (body['state'] not in ('dispatching','submitted','running','unknown') or not body.get('attempt_id')
                    or receipt.job_type!=intent['body']['request']['job_type']
                    or body.get('remote_job_id') not in (None,receipt.job_id)
                    or receipt.state not in ('submitted','running','succeeded','failed','unknown')
                    or receipt.settled_cost is not None and (type(receipt.settled_cost) is not int or receipt.settled_cost<0)):
                raise DomainError('provider_failure','Receipt does not belong to this active provider attempt')
            data=asdict(receipt)
            private_url=data.pop('result_url')
            attempt=self.store.get_object(project_id,body['attempt_id'],conn=db)
            secret=self.store.create_object(SYSTEM_PROJECT,'provider-download',
                {'project_id':project_id,'attempt':_ref(attempt),'url':private_url},SERVICE_AUTHOR,conn=db)
            recorded=self.store.create_object(project_id,'provider-receipt',{**data,'download_reference':_ref(secret),
                'attempt':_ref(attempt),'dependencies':[_ref(attempt)]},SERVICE_AUTHOR,conn=db)
            self._settle(project_id,job,receipt.settled_cost,db)
            ready=receipt.state=='succeeded' and bool(private_url) and not receipt.critical_adjustments
            state='running' if ready else 'failed' if receipt.critical_adjustments else receipt.state
            if state=='succeeded':
                state='unknown'
            changes={'state':state,'remote_job_id':receipt.job_id,'last_receipt':_ref(recorded),
                'pending_result':_ref(recorded) if ready else None,
                'lease':body['lease'] if ready else None,'next_poll_at':self.clock()+min(self.poll_delay*2**min(body.get('poll_count',0),10),300)}
            return self._write(project_id,job,changes,'job.provider_receipt',db)

    def record_unknown(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int,
                       reason: str = 'unknown_outcome') -> dict[str, Any]:
        if reason not in ('unknown_outcome','provider_failure','invalid_media','unsupported_route','forbidden'):
            raise DomainError('invalid_input','Use a bounded failure code, not raw provider text')
        with self.store.transaction() as db:
            job=self._fenced(worker,project_id,job_ref,fence,db)
            self._settle(project_id,job,None,db)
            return self._write(project_id,job,{'state':'unknown','lease':None,'last_error':reason,
                'next_poll_at':self.clock()+min(self.poll_delay*2**min(job['body'].get('poll_count',0),10),300)},'job.unknown',db)

    def poll_failed(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int) -> dict[str, Any]:
        return self.record_unknown(worker,project_id,job_ref,fence,'provider_failure')

    def _output_policy(self, intent: dict[str, Any]) -> dict[str, Any]:
        """The route profile the intent froze, checked against its frozen hash. Older intents froze only the hash; their profile is the runtime config's, which kept release 97's profiles unchanged."""
        body = intent['body']
        try:
            task = body['task']
            role, modality = ('image_routes', 'image') if task in ('image', 'image-edit') else ('video_routes', 'video')
            if task not in ('image', 'image-edit', 'shot', 'stress'):
                raise ValueError('Unsupported generation task')
            routes = self.submissions.workflow.config.section(role)
            binding = body['route']
            frozen = body.get('route_profile')
            profile = frozen if isinstance(frozen, dict) else routes['profiles'][binding['profile_id']]
            request = body['request']
            if body.get('operation') == 'complete-draft':
                # the 1080p completion of a draft take, measured by the draft route's
                # contract at 1080p with the draft's duration, ratio and audio.
                if (set(binding) != {'profile_id', 'profile_hash'} or content_hash(profile) != binding['profile_hash']
                        or profile.get('draft') is not True or request['job_type'] != profile['job_type'] + '_complete'
                        or request['params']['resolution'] != '1080p'
                        or request['params']['aspect_ratio'] not in profile['aspect_ratios']):
                    raise ValueError('Frozen completion does not match its draft route')
                policy = validate_policy(profile['output_contract'], modality=modality, resolutions=['1080p'])
                validate_request(request['params'], policy)
                return policy
            if (set(binding) != {'profile_id', 'profile_hash'} or content_hash(profile) != binding['profile_hash']
                    or not isinstance(frozen, dict) and routes['method_routes'][body['method_id']] != binding['profile_id']
                    or request['job_type'] != profile['job_type'] or task not in profile['tasks']
                    or request['params']['resolution'] not in profile['resolutions']
                    or request['params']['aspect_ratio'] not in profile['aspect_ratios']):
                raise ValueError('Frozen route does not match its release')
            policy = validate_policy(profile['output_contract'], modality=modality, resolutions=profile['resolutions'])
            validate_request(request['params'], policy)
            return policy
        except (KeyError, TypeError, ValueError):
            raise DomainError('unsupported_route', 'Delivered output requires an exact released profile and output contract') from None

    def _bind_image(self, project_id: str, intent: dict[str, Any], media: dict[str, Any], db: sqlite3.Connection) -> None:
        """a finished asset image becomes that asset's one image (HF: an element is a descriptor
        plus one image, HF_CANONICAL.md §2), with no agent call. Only while the asset is the version the image was made
        for and not under a picture lock; a changed asset keeps its images and the writer makes a new one."""
        body = intent['body']
        target = body.get('target') or {}
        if body.get('operation') != 'submit' or body.get('task') not in ('image', 'image-edit') or not target.get('object_id'):
            return
        asset = self.store.get_object(project_id, target['object_id'], conn=db)
        content = asset['body'].get('content')
        if asset['kind'] != 'asset' or asset['revision'] != target.get('revision') or not isinstance(content, dict):
            return
        try:
            self.submissions.workflow.guard_mutation(self.store, project_id, asset['object_id'], db)
        except DomainError:
            return
        ref = _ref(media)
        dependencies = [d for d in asset['body'].get('dependencies', []) if d.get('object_id') not in
                        {r.get('object_id') for r in content.get('media_refs') or []}]
        self.store.append_revision(project_id, asset['object_id'], asset['revision'],
                                   {**asset['body'], 'content': {**content, 'media_refs': [ref]}, 'dependencies': [*dependencies, ref]},
                                   SERVICE_AUTHOR, conn=db)

    def download_result(self, worker: Principal, project_id: str, job_ref: ObjectRef, fence: int) -> dict[str, Any]:
        # The attempt is counted in its own transaction before any check: a check that keeps
        # failing uses up the download allowance and the job is quarantined, instead of being re-claimed forever.
        with self.store.transaction() as db:
            job=self._fenced(worker,project_id,job_ref,fence,db)
            if not job['body'].get('pending_result'):
                raise DomainError('missing_prerequisite','No durable successful provider receipt to download')
            count=job['body'].get('download_count',0)
            remaining=job['body']['lease']['expires_at']-self.clock()-1
            if count>=self.max_downloads or remaining<=0:
                raise DomainError('attempt_limit','Download allowance or lease time exhausted')
            timeout=min(self.download_timeout,remaining)
            job=self._write(project_id,job,{'download_count':count+1},'job.downloading',db)
            job_ref=ObjectRef(**_ref(job))
        with self.store.transaction(write=False) as db:
            job=self._fenced(worker,project_id,job_ref,fence,db)
            pending=job['body']['pending_result']
            receipt=self.store.get_object(project_id,pending['object_id'],revision=pending['revision'],conn=db)
            if (receipt['author'] != SERVICE_AUTHOR or receipt['digest'] != pending['digest']
                    or receipt['kind'] != 'provider-receipt' or receipt['revision'] != 1
                    or receipt['body'].get('state') != 'succeeded' or receipt['body'].get('critical_adjustments')):
                raise DomainError('forbidden', 'Provider receipt binding changed')
            intent_ref = ObjectRef.model_validate(job['body']['intent'])
            intent = self.store.get_object(project_id, intent_ref.object_id, revision=intent_ref.revision, conn=db)
            if (intent['kind'] != 'dispatch-intent' or intent['author'] != 'submission_service'
                    or intent['revision'] != 1 or intent['digest'] != intent_ref.digest
                    or intent['body'].get('operation') not in ('submit', 'complete-draft')):
                raise DomainError('forbidden', 'Generated output lacks its exact service intent')
            attempt_ref = ObjectRef.model_validate(receipt['body']['attempt'])
            attempt = self.store.get_object(project_id, attempt_ref.object_id, revision=attempt_ref.revision, conn=db)
            if (attempt['kind'] != 'provider-attempt' or attempt['author'] != SERVICE_AUTHOR or attempt['revision'] != 1
                    or attempt['digest'] != attempt_ref.digest or job['body'].get('attempt_id') != attempt['object_id']
                    or attempt['body'].get('job_id') != job['object_id'] or attempt['body'].get('intent') != _ref(intent)
                    or attempt['body'].get('request') != intent['body']['request']):
                raise DomainError('forbidden', 'Generated output attempt differs from its frozen request')
            policy = self._output_policy(intent)
            secret_ref=ObjectRef.model_validate(receipt['body']['download_reference'])
            secret=self.store.get_object(SYSTEM_PROJECT,secret_ref.object_id,revision=secret_ref.revision,conn=db)
            if (secret['digest']!=secret_ref.digest or secret['author']!=SERVICE_AUTHOR
                    or secret['kind']!='provider-download' or secret['body']['project_id']!=project_id
                    or secret['body']['attempt']!=receipt['body']['attempt']):
                raise DomainError('forbidden','Private download binding changed')
            url=secret['body']['url']
        def publish(metadata: dict[str, Any]) -> dict[str, Any]:
            conformance = check_result(intent['body']['request']['params'], metadata['probe'], metadata['media_type'], policy)
            with self.store.transaction() as db:
                current = self._fenced(worker, project_id, job_ref, fence, db)
                if not conformance['passed']:
                    # Bytes are already durable. This private evidence is not a
                    # media object, so failed output cannot be selected or played.
                    evidence = self.store.create_object(SYSTEM_PROJECT, 'output-evidence', {
                        'project_id': project_id, 'intent': _ref(intent), 'attempt': _ref(attempt),
                        'provider_receipt': _ref(receipt), 'release_id': intent['body']['release_id'],
                        'route': intent['body']['route'], 'metadata': metadata, 'conformance': conformance,
                    }, SERVICE_AUTHOR, conn=db)
                    reservation = db.execute('SELECT state FROM reservations WHERE project_id=? AND reservation_id=?',
                        (project_id, current['body'].get('reservation_id'))).fetchone()
                    cost_status = 'settled' if reservation and reservation['state'] == 'settled' else 'unknown'
                    return self._write(project_id, current, {
                        'state': 'failed', 'result': None, 'pending_result': None, 'lease': None,
                        'output_evidence': _ref(evidence), 'cost_status': cost_status,
                        'last_error': {'code': 'delivered_output_nonconforming',
                            **{key: conformance[key] for key in ('reasons', 'requested', 'observed', 'policy_hash')}},
                    }, 'job.output_rejected', db)
                graph = self.submissions.workflow.pinned_graph(project_id, ObjectRef(**_ref(intent)), conn=db)
                # A completion is the same take at 1080p: it names the draft it completes, and
                # its origin runs through the completion intent to that draft (workflow.media_origin).
                completes = ({'completes': intent['body']['target']} if intent['body'].get('operation') == 'complete-draft' else {})
                media = self.store.create_object(project_id, 'media', {**metadata, **completes, 'dependencies': [_ref(intent)],
                    'provenance': {'intent': _ref(intent), 'attempt': _ref(attempt), 'provider_receipt': _ref(receipt),
                        'requested_parameters': intent['body']['request']['params'], 'actual_probe': metadata['probe'],
                        'output_conformance': conformance,
                        'provider_reports': {key: receipt['body'].get(key, {}) for key in
                            ('parameter_reports', 'adjustments', 'critical_adjustments')},
                        # fal drafts: the seed, and the draft id with its expiry for the 1080p completion.
                        'provider_output': {key: value for key, value in (receipt['body'].get('raw_receipt') or {}).items()
                                            if key in ('seed', 'draft_id', 'draft_expires_at')},
                        'native_resolution_verified': False}}, SERVICE_AUTHOR, conn=db)
                link_generated_repairs(self.store, self.submissions.workflow, project_id, ObjectRef(**_ref(media)), conn=db)
                self._bind_image(project_id, intent, media, db)
                return self._write(project_id, current, {'state': 'succeeded', 'result': _ref(media), 'pending_result': None,
                    'lease': None, 'current': not graph['stale'], 'non_current_reasons': graph['reasons']}, 'job.result_persisted', db)
        try:
            with self.downloader.open(url,max_bytes=self.media.max_bytes,timeout=timeout) as response:
                return self.media.put(project_id,response.chunks,response.media_type,SERVICE_AUTHOR,
                    logical_path=f"results/{job['object_id']}",publish=publish)
        except DomainError as exc:
            try:
                self.record_unknown(worker,project_id,job_ref,fence,'invalid_media' if exc.code=='invalid_media' else 'provider_failure')
            except DomainError:
                pass  # Expired/replaced fence cannot publish even the failure.
            raise

    def cancel(self, worker: Principal, project_id: str, job_ref: ObjectRef) -> dict[str, Any]:
        """Worker command only. In-flight cancellation is a request, not a refund.

        In-flight workers must re-read the current job before recording completion,
        using the same still-valid fence; cancellation never grants a new fence.
        """
        with self.store.transaction() as db:
            job=self._job(worker,project_id,job_ref,db,'reconcile-job')
            if job['body']['state'] in ('succeeded','failed','cancelled'):
                return job
            changes: dict[str,Any]={'cancel_requested':True}
            if job['body']['state']=='queued' and not job['body'].get('attempt_id'):
                self._settle(project_id,job,0,db)
                changes.update(state='cancelled',lease=None)
            return self._write(project_id,job,changes,'job.cancel_requested',db)
