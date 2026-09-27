"""Named API/viewer bootstrap. Generation and operator commands live elsewhere."""
from __future__ import annotations

import json
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError

from production.access_keys import AccessKeys
from production.api import MutationServices, Services, create_app
from production.asset_methods import AssetMethods
from production.auth import AuthService
from production.batches import Batches
from production.compiler import Compiler
from production.context import ContextService
from production.contracts import Contract, DomainError
from production.cuts import Cuts
from production.decisions import Decisions
from production.gates import Gates
from production.owner_login import LocalOwnerLogin, OwnerLogin
from production.patches import Patches
from production.projects import Projects
from production.queries import Queries
from production.reviews import Reviews
from production.runtime_config import RuntimeConfig
from production.runtime_storage import StorageConfiguration, open_storage
from production.shoot import Shoot
from production.submissions import Submissions
from production.workflow import Workflow

ROOT = Path(__file__).resolve().parents[1]


class OwnerConfiguration(Contract):
    """the one owner, as Cloudflare Access identifies him."""
    issuer: Annotated[str, Field(pattern=r'^https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$')]
    audience: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    subjects: Annotated[list[Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,256}$')]], Field(min_length=1, max_length=4)]
    # Rehearsal only: a private local JWKS file instead of the issuer's certs endpoint.
    jwks_file: str | None = None


class DefaultEnvelope(Contract):
    budget_key: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')]
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    ceiling: Annotated[int, Field(ge=0, le=10**12)]


class ServerConfiguration(Contract):
    storage: StorageConfiguration
    public_origin: str
    human_confirmation_enabled: bool = True
    live_enabled: Annotated[bool, Field(strict=True)] = False
    owner: OwnerConfiguration | None = None
    # the desk only on this Mac, without a login, at a http://localhost origin.
    local_owner: Annotated[bool, Field(strict=True)] = False
    # The owner's "收起": projects his desk does not show.
    hidden_projects: Annotated[list[Annotated[str, Field(pattern=r'^project_[A-Za-z0-9_]{1,64}$')]], Field(max_length=10000)] = []
    # Every new project gets these spending envelopes at creation (budget_key, unit, ceiling); optional.
    default_envelopes: Annotated[list[DefaultEnvelope], Field(max_length=8)] = []


def _jwks_file(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute() or '..' in target.parts or target.is_symlink():
        raise DomainError('invalid_input', 'The rehearsal JWKS file must be an explicit absolute path')
    return json.loads(target.read_text())


def load_config(path: Path) -> ServerConfiguration:
    try:
        if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > 65536):
                raise ValueError
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        config = ServerConfiguration.model_validate(json.loads(raw, object_pairs_hook=unique))
        origin = urlsplit(config.public_origin)
        if (origin.scheme not in ('https', 'http') or not origin.hostname or origin.username or origin.password
                or origin.path or origin.query or origin.fragment
                or (origin.scheme != 'https' and ((config.storage.mode != 'development' and not config.local_owner)
                    or origin.hostname not in ('localhost', '127.0.0.1', '::1')))
                or config.local_owner and (origin.scheme != 'http' or config.owner is not None)):
            raise ValueError
        return config
    except (OSError, ValueError, ValidationError):
        raise DomainError('invalid_input', 'API configuration is missing, unsafe or incompatible') from None


def build_server(config_path: Path) -> FastAPI:
    config = load_config(config_path)
    runtime = open_storage(config.storage, role='api')
    personal_readers: list[Any] = []
    try:
        store, media = runtime.store, runtime.media
        auth = AuthService(store, config.public_origin, hidden_projects=config.hidden_projects)
        owner_login: Any = LocalOwnerLogin(auth) if config.local_owner else None
        if config.owner is not None:
            oc = config.owner
            if oc.jwks_file is not None:
                path = oc.jwks_file
                owner_login = OwnerLogin(auth, issuer=oc.issuer, audience=oc.audience, subjects=oc.subjects,
                                         jwks=lambda: _jwks_file(path))
            else:
                keys = AccessKeys(oc.issuer)
                personal_readers.append(keys)
                owner_login = OwnerLogin(auth, issuer=oc.issuer, audience=oc.audience, subjects=oc.subjects,
                                         jwks=keys.get, jwks_fresh=lambda: keys.get(fresh=True))
        settings = RuntimeConfig.load()
        workflow = Workflow(store, auth, settings)
        context = ContextService(store, auth, workflow)
        gates = Gates(store, workflow, media)
        if config.default_envelopes:
            # A default envelope must fund an account the cost policy actually bills, in its unit
            # (units are immutable per project and account, store.py set_budget).
            billed = set()
            policy = settings.section('execution_policy')
            for entries in policy.get('operations', {}).values():
                for entry in entries.values():
                    if isinstance(entry, dict) and entry.get('mode') == 'live' and entry.get('budget_key'):
                        billed.add((entry['budget_key'], entry.get('budget_unit')))
            for envelope in config.default_envelopes:
                if (envelope.budget_key, envelope.unit) not in billed:
                    raise DomainError('invalid_input', f'Default envelope {envelope.budget_key} ({envelope.unit}) '
                                      'matches no live cost policy in the runtime config')
        projects = Projects(store, auth, media, label=settings.label,
                            default_envelopes=tuple((e.budget_key, e.unit, e.ceiling) for e in config.default_envelopes))
        assets = AssetMethods(store, media, workflow,
            route_profiles=settings.section('image_routes')['profiles'])
        compiler = Compiler(store, auth, workflow, context, assets,
            route_profiles=settings.section('video_routes')['profiles'])
        reviews = Reviews(store, auth, workflow, media)
        submissions = Submissions(store, auth, workflow, gates, live_enabled=config.live_enabled)
        cuts = Cuts(store, auth, workflow, media)
        batches = Batches(store, auth, workflow, submissions, reviews)
        mutations = MutationServices(projects, compiler, submissions, reviews, batches, cuts,
            Patches(store, auth, projects, workflow),
            # A decision request stays open one day (the Decisions maximum) so the owner is not rushed.
            Decisions(store, auth, workflow, cuts, ttl_seconds=86400,
                      human_confirmation_enabled=config.human_confirmation_enabled),
            Shoot(store, auth, projects, compiler, batches))
        services = Services(store, auth, Queries(store, auth, workflow), context, media, mutations)
        app = create_app(services, owner_login=owner_login)
        app.state.runtime_storage = runtime

        @asynccontextmanager
        async def lifespan(_app):
            try:
                yield
            finally:
                for reader in personal_readers:
                    reader.close()
                runtime.close()
        app.router.lifespan_context = lifespan

        @app.get('/ready')
        def ready():
            return runtime.readiness()

        # Fixed source directories, never a project path or uploaded filename.
        app.mount('/viewer/fonts', StaticFiles(directory=ROOT/'production/web/fonts'), name='viewer-fonts')
        app.mount('/viewer', StaticFiles(directory=ROOT/'production/web'), name='viewer')

        @app.get('/projects')
        @app.get('/projects/{route:path}')
        def viewer(route: str = ''):
            return FileResponse(ROOT/'production/web/index.html', media_type='text/html')

        # The bare address opens the review desk (owner 2026-09-24: mvgp.example.com showed {"detail":"Not Found"}).
        @app.get('/')
        def home():
            return RedirectResponse('/review', status_code=307)

        # Review desk (看片台): the owner's take and final decisions on the same human-decision endpoint.
        @app.get('/review')
        def review_desk():
            return FileResponse(ROOT/'production/web/review.html', media_type='text/html')

        # Project page: the project as an HF-style tree; the path segment is read by the page itself.
        @app.get('/p/{pid}')
        def project_page(pid: str):
            return FileResponse(ROOT/'production/web/project.html', media_type='text/html')
        return app
    except BaseException:
        for reader in personal_readers:
            reader.close()
        runtime.close()
        raise


def app_factory() -> FastAPI:
    """uvicorn production.server:app_factory --factory; no import-time IO."""
    value = os.environ.get('MVGP_API_CONFIG')
    if not value:
        raise DomainError('missing_prerequisite', 'MVGP_API_CONFIG must name a private service configuration')
    return build_server(Path(value))
