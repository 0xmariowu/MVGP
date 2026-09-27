"""Small authenticated HTTP facade. Read routes never invoke providers."""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Annotated, Any, TypeVar
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from production.auth import OWNER_ACTOR, AuthService, Principal, session_cookie, session_cookie_name
from production.batches import Batches
from production.compiler import Compiler
from production.context import ContextService
from production.contracts import (
    ArtifactRevision,
    AuthorReport,
    BatchRequest,
    Contract,
    CutRequest,
    DecisionRequest,
    DomainError,
    FeedbackRequest,
    HumanDecision,
    MethodSelection,
    ObjectRef,
    ObserveRequest,
    PatchRequest,
    PrepareRequest,
    ProjectCreate,
    RenderCutRequest,
    SelectTakeRequest,
    SubmitRequest,
    Update,
    UploadRequest,
    discovery,
)
from production.cuts import Cuts
from production.decisions import Decisions
from production.media import MediaStore
from production.patches import Patches
from production.projects import Projects
from production.queries import Queries, _clean
from production.reviews import Reviews
from production.shoot import Shoot, ShootRequest
from production.store import Store
from production.submissions import Submissions
from production import playbook
from production.owner_login import LocalOwnerLogin, OwnerLogin
from production.owner_notes import OwnerNotes, OwnerNoteRequest, OwnerNoteWithdraw
from production.switches import Switches, SwitchesUpdate


@dataclass(frozen=True)
class MutationServices:
    projects: Projects
    compiler: Compiler
    submissions: Submissions
    reviews: Reviews
    batches: Batches
    cuts: Cuts
    patches: Patches
    decisions: Decisions
    shoot: Shoot | None = None


@dataclass(frozen=True)
class Services:
    store: Store
    auth: AuthService
    queries: Queries
    context: ContextService
    media: MediaStore
    mutations: MutationServices | None = None

    def __post_init__(self) -> None:
        for service in (self.auth, self.queries, self.context, self.media):
            if service.store is not self.store:
                raise ValueError('HTTP services must share the authoritative store')
        if self.queries.auth is not self.auth or self.context.auth is not self.auth:
            raise ValueError('HTTP services must share credential authority')
        if self.queries.workflow is not self.context.workflow:
            raise ValueError('HTTP services must share workflow authority')
        m = self.mutations
        if m is None:
            return
        for field in fields(m):
            service = getattr(m, field.name)
            if service is None:
                continue
            if service.store is not self.store or service.auth is not self.auth:
                raise ValueError('Mutation services must share transactions and credentials')
            if hasattr(service, 'workflow') and service.workflow is not self.context.workflow:
                raise ValueError('Mutation services must share the pinned workflow')
            if hasattr(service, 'media') and service.media is not self.media:
                raise ValueError('Mutation services must share immutable media storage')
        # no AI review services are wired; the
        # remaining invariants keep one store, one workflow and one gate for every path.
        if (m.compiler.context is not self.context or m.submissions.review_check is not None
                or m.submissions.gates.store is not self.store or m.submissions.gates.media is not self.media
                or m.submissions.gates.workflow is not self.context.workflow
                or m.decisions.cuts is not m.cuts
                or m.batches.submissions is not m.submissions or m.batches.reviews is not m.reviews
                or m.patches.projects is not m.projects):
            raise ValueError('HTTP mutations require shared store, workflow and gate services')



class SessionExchange(Contract):
    # only the read-only viewer exchange remains; the owner signs in through Cloudflare Access.
    kind: Annotated[str, Field(pattern=r'^viewer$')]
    secret: Annotated[str, Field(min_length=1, max_length=256)]


class OwnerAccessLogin(Contract):
    pass


class ContextRead(Contract):
    target: ObjectRef | None = None
    method: ObjectRef | None = None
    task: Annotated[str, Field(pattern=r'^(image|image-edit|shot|stress|cut)$')] | None = None


class ReferenceRead(Contract):
    target: ObjectRef
    seconds: Annotated[float, Field(ge=0)] | None = None


class ReopenRequest(Update):
    lock: ObjectRef
    targets: Annotated[list[ObjectRef], Field(min_length=1, max_length=128)]
    reason: Annotated[str, Field(min_length=1, max_length=4000)]

    @model_validator(mode='after')
    def exact(self) -> ReopenRequest:
        if (self.expected_revision != self.lock.revision or not self.reason.strip()
                or any(ref.digest is None for ref in [self.lock, *self.targets])
                or len({ref.object_id for ref in self.targets}) != len(self.targets)):
            raise ValueError('Reopen requires an exact lock, distinct exact targets and a reason')
        return self


class CandidateRead(Contract):
    candidate: ObjectRef


T = TypeVar('T', bound=Contract)
MEDIA_CACHE_CONTROL = 'private, max-age=31536000, immutable'
MEDIA_PATH = re.compile(r'/v1/projects/[^/]+/media/[^/]+')


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON member')
        result[key] = value
    return result


async def parse_body(request: Request, schema: type[T], *, limit: int = 1048576) -> T:
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
        raise DomainError('invalid_input', 'Use an application/json request body')
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise DomainError('invalid_input', 'Request body exceeds its published bound')
    return decode_body(bytes(data), schema)


def decode_body(data: bytes | str, schema: type[T]) -> T:
    try:
        value = json.loads(data, object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
        return schema.model_validate(value)
    except (ValueError, UnicodeError, ValidationError):
        raise DomainError('invalid_input', 'Request does not match the published schema') from None


def _range(value: str | None, size: int) -> tuple[int, int, int]:
    if value is None:
        return 0, size-1, 200
    match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', value)
    if not match or not any(match.groups()) or size < 1:
        raise ValueError('Only a single satisfiable byte range is supported')
    start, end = match.groups()
    if not start:
        suffix = int(end)
        if suffix < 1:
            raise ValueError('Invalid suffix')
        return max(0, size-suffix), size-1, 206
    left, right = int(start), min(int(end), size-1) if end else size-1
    if left >= size or right < left:
        raise ValueError('Unsatisfiable range')
    return left, right, 206


def create_app(services: Services, *, owner_login: OwnerLogin | LocalOwnerLogin | None = None) -> FastAPI:
    if owner_login is not None and owner_login.auth is not services.auth:
        raise ValueError('The owner login must share the HTTP credential authority')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services
    auth, queries, store = services.auth, services.queries, services.store
    expected_host = urlsplit(auth.public_origin).netloc.lower()

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if request.headers.get('host', '').lower() != expected_host:
            return JSONResponse({'code': 'forbidden', 'message': 'Unexpected service host'}, status_code=403)
        response = await call_next(request)
        # Only successful revision-addressed media may be kept by the browser; everything else is no-store.
        cacheable = (response.headers.get('cache-control') == MEDIA_CACHE_CONTROL and response.status_code in (200, 206)
                     and MEDIA_PATH.fullmatch(request.url.path) is not None)
        response.headers.update({'Cache-Control': MEDIA_CACHE_CONTROL if cacheable else 'private, no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        status = {'unauthorized': 401, 'forbidden': 403, 'not_found': 404,
                  'revision_conflict': 409, 'idempotency_conflict': 409, 'stale_input': 409}.get(exc.code, 422)
        return JSONResponse(_clean(exc.as_dict()), status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({'code': 'invalid_input', 'message': 'Invalid route parameters'}, status_code=422)

    def actor(request: Request, *, conn: sqlite3.Connection | None = None) -> Principal:
        authorization = request.headers.get('authorization')
        cookie = request.cookies.get(session_cookie_name(auth.public_origin))
        if authorization:
            if cookie or not authorization.startswith('Bearer ') or authorization.count(' ') != 1:
                raise DomainError('unauthorized', 'Use one authentication channel')
            return auth.authenticate(authorization[7:], conn=conn)
        if cookie:
            return auth.authenticate(cookie, channel='cookie', conn=conn)
        raise DomainError('unauthorized', 'A scoped service session is required')

    app.state.actor = actor
    mutation_routes: list[dict[str, Any]] = []

    def authorized_actor(request: Request, pid: str | None, operation: str) -> Principal:
        # Initial admission shares one fresh read. Mutations still reauthorize
        # in their own write transaction after body parsing and preparation.
        with store.transaction(write=False) as db:
            principal = actor(request, conn=db)
            auth.authorize(principal, pid, operation, conn=db)
            return principal

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/v1/session/access')
    async def access_login(request: Request):
        # Cloudflare Access proves the person; only a configured owner gets a desk session.
        if owner_login is None:
            raise DomainError('missing_prerequisite', 'The owner login is not configured')
        await parse_body(request, OwnerAccessLogin, limit=4096)
        if request.headers.get('authorization'):
            raise DomainError('forbidden', 'The owner signs in with the Access identity, not a bearer credential')
        result = await run_in_threadpool(lambda: owner_login.login(
            request.headers.get('cf-access-jwt-assertion', ''), request.headers.get('origin', ''),
            fetch_site=request.headers.get('sec-fetch-site', '')))
        response = JSONResponse({k: result[k] for k in ('actor', 'csrf_token')})
        response.headers['Set-Cookie'] = result['cookie']
        return response

    @app.post('/v1/session/exchange')
    async def exchange(request: Request):
        body = await parse_body(request, SessionExchange, limit=4096)
        origin = request.headers.get('origin', '')
        result = await run_in_threadpool(auth.exchange_viewer, body.secret, origin)
        response = JSONResponse({'role': body.kind})
        response.headers['Set-Cookie'] = result['cookie']
        return response

    @app.get('/v1/session')
    def session(request: Request):
        with store.transaction(write=False) as db:
            principal = actor(request, conn=db)
            auth.authorize(principal, None, 'projects', conn=db)
            # the desk renews the session before it lapses (media URLs need the cookie).
            return {'actor_id': principal.actor_id, 'role': principal.role,
                    'projects': auth.visible_projects(principal, conn=db), 'expires_at': principal.expires_at}

    @app.post('/v1/session/logout')
    def logout(request: Request):
        principal = actor(request)
        if principal.channel != 'cookie' or request.headers.get('origin') != auth.public_origin:
            raise DomainError('forbidden', 'Logout requires the session origin')
        auth.revoke(principal.credential_id)
        response = JSONResponse({'logged_out': True})
        response.headers['Set-Cookie'] = session_cookie(auth.public_origin, '', 0)
        return response

    @app.get('/v1/discovery')
    def discover(request: Request):
        auth.authorize(actor(request), None, 'discovery')
        return {'contract': discovery(), 'read_schemas': {'context': ContextRead.model_json_schema(),
                'reference': ReferenceRead.model_json_schema(), 'inspect': ContextRead.model_json_schema(),
                'candidate': CandidateRead.model_json_schema()}, 'production_mutations_available': services.mutations is not None,
                'mutation_routes': mutation_routes}

    @app.get('/v1/projects')
    def projects(request: Request):
        with store.transaction(write=False) as db:
            return queries.list_projects(actor(request, conn=db), conn=db)

    def owner_only(principal: Principal) -> None:
        """The 账本 is the owner's: only his desk session reads it, not an agent or viewer."""
        if principal.role != 'human' or principal.actor_id != OWNER_ACTOR:
            raise DomainError('forbidden', 'The ledger is read in the owner\'s own desk session')

    def ledger_view(principal: Principal, pids: list[str], db: sqlite3.Connection) -> dict[str, Any]:
        """Every paid call of these projects, and each of the last 7 days next to fal's and apilio's own records."""
        import datetime as dt

        from production.billing import ledger, reconcile
        rows = []
        for pid in pids:
            auth.authorize(principal, pid, 'context', conn=db)
            rows += [{'project_id': pid, **row} for row in ledger(store, pid, conn=db)['rows']]
        today = dt.datetime.now(dt.timezone.utc).date()
        folder = store.path.parent / 'ledger'
        days = [reconcile(store, pids, (today - dt.timedelta(days=n)).isoformat(), folder, conn=db) for n in range(7)]
        return {'rows': rows, 'days': days}

    @app.get('/v1/ledger')
    def ledger_all(request: Request):
        with store.transaction(write=False) as db:
            principal = actor(request, conn=db)
            owner_only(principal)
            return ledger_view(principal, sorted(auth.visible_projects(principal, conn=db)), db)

    @app.get('/v1/projects/{pid}/ledger')
    def ledger_project(pid: str, request: Request):
        with store.transaction(write=False) as db:
            principal = actor(request, conn=db)
            owner_only(principal)
            return ledger_view(principal, [pid], db)

    @app.get('/v1/projects/{pid}')
    def project(pid: str, request: Request):
        with store.transaction(write=False) as db:
            return queries.project(actor(request, conn=db), pid, conn=db)

    @app.get('/v1/projects/{pid}/review-feed')
    def review_feed(pid: str, request: Request):
        with store.transaction(write=False) as db:
            return queries.review_feed(actor(request, conn=db), pid, conn=db)

    @app.get('/v1/projects/{pid}/project-tree')
    def project_tree(pid: str, request: Request):
        with store.transaction(write=False) as db:
            return queries.project_tree(actor(request, conn=db), pid, conn=db)

    @app.get('/v1/projects/{pid}/playbook')
    def project_playbook(pid: str, request: Request):
        # the writer's manuals, and a record of who took which version.
        with store.transaction() as db:
            principal = actor(request, conn=db)
            auth.authorize(principal, pid, 'context', conn=db)
            return playbook.hand_over(store, principal, pid, conn=db)

    @app.get('/v1/projects/{pid}/generation-record')
    def generation_record(pid: str, request: Request):
        with store.transaction(write=False) as db:
            return queries.generation_record(actor(request, conn=db), pid, conn=db)

    owner_switches = Switches(store, services.auth, queries.workflow.config)

    @app.get('/v1/projects/{pid}/switches')
    def switches_view(pid: str, request: Request):
        # the owner's per-film switches (the stress test is the live one).
        return owner_switches.get(actor(request), pid)

    @app.get('/v1/projects/{pid}/artifacts/{oid}')
    def artifact(pid: str, oid: str, request: Request, revision: int | None = None):
        with store.transaction(write=False) as db:
            return queries.artifact(actor(request, conn=db), pid, oid, revision, conn=db)

    @app.get('/v1/projects/{pid}/artifacts/{oid}/history')
    def history(pid: str, oid: str, request: Request):
        principal = actor(request)
        queries.artifact(principal, pid, oid)
        return [queries.artifact(principal, pid, oid, item['revision']) for item in store.history(pid, oid)]

    @app.get('/v1/projects/{pid}/tree')
    def tree(pid: str, request: Request, parent: str = ''):
        with store.transaction(write=False) as db:
            return queries.tree(actor(request, conn=db), pid, parent, conn=db)

    @app.post('/v1/projects/{pid}/context')
    async def context(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        body = await parse_body(request, ContextRead)
        return await run_in_threadpool(services.context.get, principal, pid, target=body.target, task=body.task, method=body.method)

    @app.get('/v1/projects/{pid}/methods')
    def methods(pid: str, request: Request):
        return services.context.methods(actor(request), pid)

    @app.get('/v1/projects/{pid}/references/{role}')
    def document(pid: str, role: str, request: Request):
        return services.context.document(actor(request), pid, role)

    @app.post('/v1/projects/{pid}/reference')
    async def reference(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        body = await parse_body(request, ReferenceRead)
        return await run_in_threadpool(queries.copy_reference, principal, pid, body.target, body.seconds)

    @app.api_route('/v1/projects/{pid}/media/{oid}', methods=['GET', 'HEAD'])
    def media(pid: str, oid: str, request: Request, revision: int):
        with store.transaction(write=False) as db:
            principal = actor(request, conn=db)
            auth.authorize(principal, pid, 'media', conn=db)
            item = queries.artifact(principal, pid, oid, revision, conn=db)
            if item['kind'] != 'media':
                raise DomainError('not_found', 'Requested artifact is not media')
        path = services.media.path_for(pid, oid, revision=revision)
        size = path.stat().st_size
        body = item['details']
        headers = {'Accept-Ranges': 'bytes', 'ETag': '"'+body['sha256']+'"'}
        try:
            start, end, status = _range(request.headers.get('range'), size)
        except ValueError:
            return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{size}'})
        # The URL names one object revision whose bytes are immutable (sha256 above).
        headers['Cache-Control'] = MEDIA_CACHE_CONTROL
        headers['Content-Length'] = str(end-start+1)
        if status == 206:
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        if request.method == 'HEAD':
            return Response(status_code=status, headers=headers, media_type=body['media_type'])
        def chunks():
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise OSError('Immutable media became incomplete')
                    remaining -= len(chunk)
                    yield chunk
        return StreamingResponse(chunks(), status_code=status, headers=headers, media_type=body['media_type'])

    m = services.mutations
    if m is None:
        return app

    def public(principal: Principal, pid: str, value: Any) -> Any:
        # A fresh post-operation snapshot, never the preauthorization snapshot.
        # Project all returned records consistently without one restore per item.
        with store.transaction(write=False) as db:
            return public_in_snapshot(principal, pid, value, db)

    def public_in_snapshot(principal: Principal, pid: str, value: Any, db: sqlite3.Connection) -> Any:
        # Domain results can contain private journals. Project each actual object
        # through the same read boundary; never expose raw service bodies.
        if isinstance(value, dict):
            if {'object_id', 'revision', 'digest', 'kind', 'body'} <= value.keys():
                if value['kind'] == 'project':
                    return {'object_ref': {key: value[key] for key in ('object_id', 'revision', 'digest')},
                            **queries.project(principal, value['object_id'], conn=db)}
                return queries.artifact(principal, pid, value['object_id'], value['revision'], conn=db)
            return _clean({key: public_in_snapshot(principal, pid, item, db) for key, item in value.items()})
        if isinstance(value, list):
            return [public_in_snapshot(principal, pid, item, db) for item in value]
        return _clean(value)

    def describe(path: str, schema: type[Contract], operation: str, method: str = 'POST') -> None:
        mutation_routes.append({'path': path, 'method': method, 'operation': operation,
                                'schema': schema.model_json_schema(), 'synchronous_paid_work': False})

    def bind(path: str, schema: type[Contract], operation: str, call: Callable[..., Any], *, queued: bool = False) -> None:
        # Closed route registrations below, not a client-selectable dispatcher.
        async def endpoint(pid: str, request: Request):
            principal = await run_in_threadpool(authorized_actor, request, pid, operation)
            body = await parse_body(request, schema)
            result = await run_in_threadpool(call, principal, pid, body)
            return await run_in_threadpool(public, principal, pid, result)
        full_path = '/v1/projects/{pid}' + path
        app.add_api_route(full_path, endpoint, methods=['POST'], status_code=202 if queued else 200)
        describe(full_path, schema, operation)

    @app.post('/v1/projects', status_code=201)
    async def create_project(request: Request):
        principal = await run_in_threadpool(authorized_actor, request, None, 'create-project')
        body = await parse_body(request, ProjectCreate)
        result = await run_in_threadpool(m.projects.create, principal, body)
        return await run_in_threadpool(public, principal, result['object_id'], result)
    describe('/v1/projects', ProjectCreate, 'create-project')

    @app.put('/v1/projects/{pid}/artifacts/{oid}')
    async def revise(pid: str, oid: str, request: Request):
        principal = await run_in_threadpool(authorized_actor, request, pid, 'revise-artifact')
        body = await parse_body(request, ArtifactRevision)
        result = await run_in_threadpool(m.projects.revise, principal, pid, body, artifact_id=oid)
        return await run_in_threadpool(public, principal, pid, result)
    describe('/v1/projects/{pid}/artifacts/{oid}', ArtifactRevision, 'revise-artifact', 'PUT')

    def reopen(principal: Principal, pid: str, body: ReopenRequest) -> dict[str, Any]:
        with store.transaction() as db:
            auth.authorize(principal, pid, 'patch', conn=db)
            return store.run_idempotent(f'{pid}:{principal.credential_id}:reopen', body.idempotency_key,
                body.model_dump(), lambda tx: services.context.workflow.reopen(principal, pid, body.lock,
                    body.targets, body.reason, conn=tx), conn=db)

    for path, schema, operation, call, queued in (
        ('/artifacts', ArtifactRevision, 'revise-artifact', m.projects.revise, False),
        ('/method-selections', MethodSelection, 'select-method', m.projects.select_method, False),
        ('/candidates', PrepareRequest, 'prepare', m.compiler.prepare, False),
        ('/submissions', SubmitRequest, 'submit', m.submissions.submit, True),
        ('/observations', ObserveRequest, 'observe', m.submissions.observe, True),
        ('/batches', BatchRequest, 'batch', m.batches.create, True),
        ('/take-selections', SelectTakeRequest, 'select-take', m.reviews.select_take, False),
        ('/cuts', CutRequest, 'create-cut', m.cuts.create, False),
        ('/cuts/render', RenderCutRequest, 'render-cut', m.submissions.render_cut, True),
        ('/feedback', FeedbackRequest, 'feedback', m.reviews.feedback, False),
        ('/reports', AuthorReport, 'report', m.reviews.report, False),
        ('/patches', PatchRequest, 'patch', m.patches.apply, False),
        ('/decision-requests', DecisionRequest, 'request-decision', m.decisions.request, False),
        ('/reopens', ReopenRequest, 'patch', reopen, False),
    ):
        bind(path, schema, operation, call, queued=queued)
    if m.shoot is not None:
        # the agent's one call after the cards are written.
        bind('/shoot-orders', ShootRequest, 'prepare', m.shoot.order)
        shoot = m.shoot

        @app.get('/v1/projects/{pid}/quote')
        def quote(pid: str, request: Request, card: Annotated[list[str] | None, Query()] = None,
                  takes: Annotated[int, Query(ge=1, le=4)] = 4):
            """what shooting these cards (default: every shot card) would cost; read-only."""
            principal = actor(request)
            with store.transaction(write=False) as db:
                ids = card or [o['object_id'] for o in store.list_objects(pid, kind='shot', conn=db)]
                refs = [ObjectRef(**{k: o[k] for k in ('object_id', 'revision', 'digest')})
                        for o in (store.get_object(pid, oid, conn=db) for oid in ids)]
            return public(principal, pid, shoot.quote(principal, pid, refs, takes))

    @app.post('/v1/projects/{pid}/uploads', status_code=201)
    async def upload(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        await run_in_threadpool(auth.authorize, principal, pid, 'upload')
        metadata = request.headers.getlist('x-mvgp-upload')
        if (len(metadata) != 1 or len(metadata[0].encode()) > 16384
                or request.headers.get('content-type', '').lower() != 'application/octet-stream'):
            raise DomainError('invalid_input', 'Upload needs one bounded X-MVGP-Upload JSON header and application/octet-stream')
        body = decode_body(metadata[0], UploadRequest)
        length = request.headers.get('content-length')
        if body.byte_length > services.media.max_bytes or (length is not None and length != str(body.byte_length)):
            raise DomainError('invalid_input', 'Upload length differs from its metadata or storage bound')
        # ASGI input is async, the domain store consumes a synchronous iterable.
        # Spool to private disk with a fixed byte bound, never retain the film in RAM.
        with tempfile.TemporaryFile(mode='w+b') as spool:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > body.byte_length:
                    raise DomainError('invalid_input', 'Upload exceeds its exact declared length')
                await run_in_threadpool(spool.write, chunk)
            if size != body.byte_length:
                raise DomainError('invalid_input', 'Upload ended before its exact declared length')
            spool.seek(0)
            result = await run_in_threadpool(m.projects.upload, principal, pid, body, iter(lambda: spool.read(65536), b''))
        return await run_in_threadpool(public, principal, pid, result)
    describe('/v1/projects/{pid}/uploads', UploadRequest, 'upload')
    mutation_routes[-1].update(body_encoding='binary', metadata_header='X-MVGP-Upload',
                              content_type='application/octet-stream', max_bytes=services.media.max_bytes)

    @app.post('/v1/projects/{pid}/human-decisions')
    async def human_decision(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        body = await parse_body(request, HumanDecision)
        result = await run_in_threadpool(m.decisions.decide, principal, pid, body, origin=request.headers.get('origin', ''))
        return await run_in_threadpool(public, principal, pid, result)
    describe('/v1/projects/{pid}/human-decisions', HumanDecision, 'human-decision')

    @app.post('/v1/projects/{pid}/switches')
    async def switches_update(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        body = await parse_body(request, SwitchesUpdate)
        return await run_in_threadpool(owner_switches.set, principal, pid, body, origin=request.headers.get('origin', ''))
    describe('/v1/projects/{pid}/switches', SwitchesUpdate, 'human-decision')

    # the owner's notes, through the same human session + origin + CSRF check as a pick.
    owner_notes = OwnerNotes(store, services.auth)

    @app.post('/v1/projects/{pid}/owner-notes')
    async def owner_note_add(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        body = await parse_body(request, OwnerNoteRequest)
        result = await run_in_threadpool(owner_notes.add, principal, pid, body, origin=request.headers.get('origin', ''))
        return {'object_ref': {k: result[k] for k in ('object_id', 'revision', 'digest')}, 'withdrawn': False}
    describe('/v1/projects/{pid}/owner-notes', OwnerNoteRequest, 'human-decision')

    @app.post('/v1/projects/{pid}/owner-notes/withdraw')
    async def owner_note_withdraw(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        body = await parse_body(request, OwnerNoteWithdraw)
        result = await run_in_threadpool(owner_notes.withdraw, principal, pid, body, origin=request.headers.get('origin', ''))
        return {'object_ref': {k: result[k] for k in ('object_id', 'revision', 'digest')}, 'withdrawn': True}
    describe('/v1/projects/{pid}/owner-notes/withdraw', OwnerNoteWithdraw, 'human-decision')

    @app.get('/v1/projects/{pid}/batches/{bid}')
    def batch(pid: str, bid: str, request: Request):
        principal = actor(request)
        return public(principal, pid, m.batches.get(principal, pid, bid))

    @app.post('/v1/projects/{pid}/batches/{bid}/selections')
    async def batch_select(pid: str, bid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        await run_in_threadpool(auth.authorize, principal, pid, 'select-take')
        body = await parse_body(request, SelectTakeRequest)
        result = await run_in_threadpool(m.batches.select_take, principal, pid, bid, body)
        return await run_in_threadpool(public, principal, pid, result)
    describe('/v1/projects/{pid}/batches/{bid}/selections', SelectTakeRequest, 'select-take')

    @app.post('/v1/projects/{pid}/inspect')
    async def inspect(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        body = await parse_body(request, ContextRead)
        result = await run_in_threadpool(services.context.workflow.inspect, principal, pid,
                                       target=body.target, task=body.task, method=body.method)
        return _clean(result)

    @app.post('/v1/projects/{pid}/candidates/inspect')
    async def candidate_inspect(pid: str, request: Request):
        principal = await run_in_threadpool(actor, request)
        await run_in_threadpool(auth.authorize, principal, pid, 'candidates')
        body = await parse_body(request, CandidateRead)
        if body.candidate.digest is None:
            raise DomainError('invalid_input', 'Candidate inspection requires its exact hash')
        return _clean(await run_in_threadpool(m.submissions.gates.evaluate, pid, body.candidate))

    return app
