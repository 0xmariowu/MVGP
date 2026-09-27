"""Service-owned credentials, scoped actions and the owner's desk session.

Provisioning and revocation are operator/worker Python boundaries, never exposed as author API
operations. The only human session comes from the owner's Access login (owner_login.py); there is no operator-issued human exchange. `platform_system` is a reserved internal
project and MUST be excluded from production project listing. Local tests do not
prove that an author's browser cannot access a human's real session/device.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from production.contracts import OPERATIONS, DomainError, canonical_json, new_id
from production.store import Store

SYSTEM_PROJECT = "platform_system"
# The only actor whose human session is the owner's desk: owner_login issues it.
OWNER_ACTOR = 'owner'
SESSION_SECONDS = 1800
READ_OPERATIONS = frozenset({"projects", "context", "artifacts", "media", "candidates", "jobs", "reviews", "cuts", "methods", "references", "discovery"})
AUTHOR_OPERATIONS = frozenset(OPERATIONS) - {"create-project"}
WORKER_OPERATIONS = frozenset({"dispatch", "record-provider-result", "issue-reviewer", "record-observation", "reconcile-job",
                               "complete-draft"})
ROLES = frozenset({"agent", "viewer", "human", "reviewer", "worker"})


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    actor_id: str
    credential_id: str
    role: str
    project_ids: frozenset[str]
    expires_at: float
    channel: str
    targets: frozenset[str] = frozenset()
    review_task_id: str | None = None
    proof: str = field(default="", repr=False)


def session_cookie(public_origin: str, token: str, max_age: int) -> str:
    """The desk session cookie. HTTPS origins use the __Host- prefix and Secure; the local http://localhost desk
    cannot carry either, so it uses a plain name. Both are HttpOnly and SameSite=Strict."""
    if public_origin.startswith('https://'):
        return f'__Host-mvgp_session={token}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Strict'
    return f'mvgp_session={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict'


def session_cookie_name(public_origin: str) -> str:
    return '__Host-mvgp_session' if public_origin.startswith('https://') else 'mvgp_session'


class AuthService:
    def __init__(self, store: Store, public_origin: str, *, clock: Callable[[], float] = time.time,
                 hidden_projects: Iterable[str] = ()) -> None:
        parsed = urlsplit(public_origin)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password
                or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise ValueError("A single HTTPS public origin (or explicit loopback development origin) is required")
        self.store, self.public_origin, self.clock = store, public_origin, clock
        # the owner's desk session sees every production project except these (his "收起").
        self.hidden_projects = frozenset(hidden_projects)
        self._signing_key = secrets.token_bytes(32)
        with store.transaction(write=False) as conn:
            initialized = conn.execute("SELECT 1 FROM projects WHERE project_id=?", (SYSTEM_PROJECT,)).fetchone() is not None
        if not initialized:
            # Recheck under writer admission: another service can initialize
            # between the readonly probe and this first-bootstrap transaction.
            with store.transaction() as conn:
                if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (SYSTEM_PROJECT,)).fetchone() is None:
                    store.create_project(SYSTEM_PROJECT, {"internal": "credential store"}, "operator", conn=conn)

    def _projects(self, project_ids: Iterable[str], conn: sqlite3.Connection) -> list[str]:
        projects = sorted(set(project_ids))
        if SYSTEM_PROJECT in projects:
            raise DomainError("forbidden", "Internal service data cannot be granted")
        for project_id in projects:
            self.store.get_object(project_id, project_id, conn=conn)
        return projects

    def _issue(self, actor_id: str, role: str, projects: list[str], ttl_seconds: int, kind: str,
               conn: sqlite3.Connection, **fields: Any) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", actor_id):
            raise DomainError("invalid_input", "Invalid actor identifier")
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 90 * 86400:
            raise DomainError("invalid_input", "Credential TTL must be a bounded positive integer")
        credential_id = new_id("credential")
        token = f"{credential_id}.{secrets.token_urlsafe(32)}"
        body = {"actor_id": actor_id, "role": role, "project_ids": projects,
                "expires_at": float(self.clock() + ttl_seconds), "revoked": False,
                "token_hash": _hash(token), **fields}
        self.store.create_object(SYSTEM_PROJECT, kind, body, "operator", object_id=credential_id, conn=conn)
        return token

    def provision_token(self, actor_id: str, role: str, project_ids: Iterable[str], ttl_seconds: int, *,
                        targets: Iterable[str] = (), review_task_id: str | None = None,
                        allow_create_project: bool = False, all_projects: bool = False,
                        conn: sqlite3.Connection | None = None) -> str:
        """Operator/worker-only provisioning. Returns a secret exactly once.

        ``all_projects`` (one owner): the credential's scope is every production project,
        re-read on each check, so a new project needs no re-issued worker or film credential. Only the worker and
        a non-creating agent (the film service) may hold it; a creator agent never does.
        """
        if role not in ROLES - {"human"} or type(allow_create_project) is not bool or type(all_projects) is not bool:
            raise DomainError("forbidden", "Role cannot be issued as a bearer credential")
        if all_projects and (role not in ("worker", "agent") or allow_create_project):
            raise DomainError("forbidden", "Only the worker and a non-creating agent may see every project")
        target_list = sorted(set(targets))
        if any(not isinstance(target, str) or not target or len(target) > 256 or "*" in target for target in target_list):
            raise DomainError("invalid_input", "Review evidence targets must be exact, nonempty references")
        if role == "reviewer":
            if (not target_list or not review_task_id or allow_create_project
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", review_task_id)):
                raise DomainError("invalid_input", "Reviewer needs exact evidence targets and one task")
        elif target_list or review_task_id:
            raise DomainError("invalid_input", "Review task scopes apply only to reviewers")
        if allow_create_project and role != "agent":
            raise DomainError("forbidden", "Only authors may receive project creation capability")
        with self.store._using(conn) as db:
            projects = self._projects(project_ids, db)
            if role == "reviewer" and len(projects) != 1:
                raise DomainError("invalid_input", "Reviewer must belong to exactly one project")
            return self._issue(actor_id, role, projects, ttl_seconds, "credential", db,
                               targets=target_list, review_task_id=review_task_id,
                               allow_create_project=allow_create_project,
                               **({"all_projects": True} if all_projects else {}))

    def _credential(self, token: str, conn: sqlite3.Connection) -> dict[str, Any]:
        if not isinstance(token, str) or not re.fullmatch(r"credential_[0-9a-f]{32}\.[A-Za-z0-9_-]{40,64}", token):
            raise DomainError("unauthorized", "Credential is invalid or expired")
        try:
            record = self.store.get_object(SYSTEM_PROJECT, token.split(".")[0], conn=conn)
        except DomainError:
            raise DomainError("unauthorized", "Credential is invalid or expired") from None
        body = record["body"]
        if (not hmac.compare_digest(body["token_hash"], _hash(token)) or body["revoked"]
                or self.clock() >= body["expires_at"]):
            raise DomainError("unauthorized", "Credential is invalid or expired")
        return record

    def _viewer_parent(self, record: dict[str, Any], conn: sqlite3.Connection) -> None:
        if record['kind'] != 'viewer-session':
            return
        body = record['body']
        try:
            parent = self.store.get_object(SYSTEM_PROJECT, body['parent_credential'], conn=conn)
        except (KeyError, DomainError):
            raise DomainError('unauthorized', 'Viewer session parent is unavailable') from None
        original = parent['body']
        if (parent['kind'] != 'credential' or original['role'] != 'viewer'
                or body['role'] != 'viewer' or original['actor_id'] != body['actor_id']
                or original['revoked'] or self.clock() >= original['expires_at']
                or original.get('member_id') != body.get('member_id')
                or set(original['project_ids']) != set(body['project_ids'])):
            raise DomainError('unauthorized', 'Viewer session parent expired or changed')

    def exchange_viewer(self, secret: str, origin: str, *,
                        conn: sqlite3.Connection | None = None) -> dict[str, str]:
        """Read-only cookie for native media; this never grants human decisions."""
        if origin != self.public_origin:
            raise DomainError('forbidden', 'Viewer exchange requires the configured origin')
        with self.store._using(conn) as db:
            parent = self._credential(secret, db)
            body = parent['body']
            if parent['kind'] != 'credential' or body['role'] != 'viewer':
                raise DomainError('forbidden', 'Only a viewer credential can open a viewer session')
            ttl = min(SESSION_SECONDS, int(body['expires_at'] - self.clock()))
            if ttl < 1:
                raise DomainError('unauthorized', 'Viewer credential is expiring')
            token = self._issue(body['actor_id'], 'viewer', body['project_ids'], ttl,
                                'viewer-session', db, parent_credential=parent['object_id'],
                                **({'member_id':body['member_id']} if 'member_id' in body else {}))
            return {'session_token': token,
                    'cookie': session_cookie(self.public_origin, token, ttl)}

    def _proof(self, principal: Principal) -> str:
        values = {"actor": principal.actor_id, "credential": principal.credential_id,
                  "role": principal.role, "projects": sorted(principal.project_ids),
                  "expires": principal.expires_at, "channel": principal.channel,
                  "targets": sorted(principal.targets), "task": principal.review_task_id}
        return hmac.new(self._signing_key, canonical_json(values).encode(), hashlib.sha256).hexdigest()

    def authenticate(self, secret: str, *, channel: str = "bearer", conn: sqlite3.Connection | None = None) -> Principal:
        with self.store._using(conn, write=False) as db:
            record = self._credential(secret, db)
            if ((channel == "bearer" and record["kind"] != "credential")
                    or (channel == "cookie" and record["kind"] not in ("human-session", "viewer-session"))
                    or channel not in ("bearer", "cookie")):
                raise DomainError("unauthorized", "Credential is not valid for this channel")
            self._viewer_parent(record, db)
            body = record["body"]
            principal = Principal(body["actor_id"], record["object_id"], body["role"],
                                  frozenset(body["project_ids"]), body["expires_at"], channel,
                                  frozenset(body.get("targets", [])), body.get("review_task_id"))
            return replace(principal, proof=self._proof(principal))

    def _fresh(self, principal: Principal, conn: sqlite3.Connection) -> dict[str, Any]:
        if not isinstance(principal, Principal) or not hmac.compare_digest(principal.proof, self._proof(principal)):
            raise DomainError("unauthorized", "Principal was not authenticated by this service")
        try:
            record = self.store.get_object(SYSTEM_PROJECT, principal.credential_id, conn=conn)
        except DomainError:
            raise DomainError("unauthorized", "Credential is invalid or expired") from None
        body = record["body"]
        if body["revoked"] or self.clock() >= body["expires_at"]:
            raise DomainError("unauthorized", "Credential is invalid or expired")
        self._viewer_parent(record, conn)
        if body["role"] != principal.role or body["actor_id"] != principal.actor_id:
            raise DomainError("unauthorized", "Credential authority changed")
        return record

    def authorize(self, principal: Principal, project_id: str | None, operation: str, *,
                  target: str | None = None, origin: str | None = None,
                  csrf_token: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        """Re-read revocation/scopes in the SAME transaction as the protected write."""
        with self.store._using(conn, write=False) as db:
            body = self._fresh(principal, db)["body"]
            if project_id == SYSTEM_PROJECT:
                raise DomainError("forbidden", "Internal service data is not a production project")
            if operation == "create-project":
                if principal.role == "agent" and body.get("allow_create_project") and project_id is None:
                    return
                raise DomainError("forbidden", "Project creation capability is absent")
            if project_id is None:
                if operation in ("projects", "methods", "discovery") and principal.role in ("agent", "viewer", "human"):
                    return
                raise DomainError("forbidden", "A project scope is required")
            if project_id not in self._visible_scope(body, db):
                raise DomainError("forbidden", "Project is outside credential scope")
            if principal.role in ("agent", "viewer", "human") and operation in READ_OPERATIONS:
                return
            if principal.role == "agent" and operation in AUTHOR_OPERATIONS:
                return
            if principal.role == "human" and operation == "human-decision":
                if (principal.channel != "cookie" or principal.actor_id != OWNER_ACTOR or origin != self.public_origin or not csrf_token
                        or not hmac.compare_digest(body["csrf_hash"], _hash(csrf_token))):
                    raise DomainError("forbidden", "Human decision needs its session, exact origin and CSRF token")
                return
            if principal.role == "reviewer" and ((operation == "review-evidence" and target in body["targets"])
                    or (operation == "record-review" and target == body["review_task_id"])):
                return
            if principal.role == "worker" and operation in WORKER_OPERATIONS:
                return
            raise DomainError("forbidden", "Role cannot perform this operation")

    def require_agent_origin(self, project_id: str, origin: dict[str, Any], *,
                             conn: sqlite3.Connection | None = None) -> None:
        """Service-only delayed dispatch check; never issues caller authority.

        Call only after validating an immutable service intent/task, in the
        same transaction as dispatch. A stored origin is not a bearer token.
        """
        with self.store._using(conn, write=False) as db:
            try:
                record = self.store.get_object(SYSTEM_PROJECT, origin['credential_id'], conn=db)
                body = record['body']
                if (record['kind'] != 'credential' or body['revoked']
                        or self.clock() >= body['expires_at'] or body['role'] != 'agent'
                        or origin['role'] != 'agent' or body['actor_id'] != origin['actor_id']):
                    raise DomainError('unauthorized', 'Queued maker authority is unavailable')
            except KeyError:
                raise DomainError('unauthorized', 'Queued maker authority is unavailable') from None
            except DomainError as exc:
                if exc.code != 'not_found':
                    raise
                raise DomainError('unauthorized', 'Queued maker authority is unavailable') from None
            if project_id == SYSTEM_PROJECT or project_id not in self._visible_scope(body, db):
                raise DomainError('unauthorized', 'Queued maker project authority is unavailable')

    def _visible_scope(self, body: dict[str, Any], db: sqlite3.Connection) -> list[str]:
        """What a credential may see now. Service credentials with all_projects and the owner's desk session see
        every production project (the desk minus the hidden ones); every other credential sees its own list."""
        everything = [row[0] for row in db.execute('SELECT project_id FROM projects WHERE project_id != ? ORDER BY project_id',
                                                   (SYSTEM_PROJECT,))]
        if (body.get('all_projects') is True and not body.get('allow_create_project')
                and body.get('role') in ('worker', 'agent')):
            return everything
        if body.get('role') == 'human':
            # Only the owner's desk session sees the desk; a human session from before the lean platform (an
            # employee member or an operator exchange) sees nothing.
            if body.get('actor_id') != OWNER_ACTOR or body.get('member_id'):
                return []
            return [pid for pid in everything if pid not in self.hidden_projects]
        return list(body['project_ids'])

    def dispatch_scope(self, principal: Principal, *, conn: sqlite3.Connection | None = None) -> list[str]:
        """The projects a worker or film credential may act on now (fresh; grows with new projects)."""
        with self.store._using(conn, write=False) as db:
            body = self._fresh(principal, db)["body"]
            if principal.role not in ("worker", "agent"):
                raise DomainError("forbidden", "Only service credentials have a dispatch scope")
            return self._visible_scope(body, db)

    def visible_projects(self, principal: Principal, *, conn: sqlite3.Connection | None = None) -> list[str]:
        """Use this fresh list, not cached Principal.project_ids, when listing projects."""
        with self.store._using(conn, write=False) as db:
            self.authorize(principal, None, "projects", conn=db)
            return self._visible_scope(self._fresh(principal, db)["body"], db)

    def revoke(self, credential_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """Operator-only; no token material is needed or recorded by revocation."""
        with self.store._using(conn) as db:
            record = self.store.get_object(SYSTEM_PROJECT, credential_id, conn=db)
            if record["kind"] not in ("credential", "human-exchange", "human-session", "viewer-session"):
                raise DomainError("invalid_input", "Target is not a credential")
            if not record["body"]["revoked"]:
                self.store.append_revision(SYSTEM_PROJECT, credential_id, record["revision"],
                                           {**record["body"], "revoked": True}, "operator", conn=db)

    def grant_created_project(self, principal: Principal, project_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        """Trusted create-project handler only, atomic with server-owned project creation."""
        with self.store._using(conn) as db:
            self.authorize(principal, None, "create-project", conn=db)
            project = self.store.get_object(project_id, project_id, conn=db)
            if (project_id == SYSTEM_PROJECT or project["kind"] != "project"
                    or project["revision"] != 1 or project["author"] != principal.actor_id):
                raise DomainError("forbidden", "Only this author's newly created project may be granted")
            credential = self._fresh(principal, db)
            body = credential["body"]
            # The owner's desk sees every project; only the creator's own scope grows.
            if project_id not in body["project_ids"]:
                self.store.append_revision(SYSTEM_PROJECT, credential["object_id"], credential["revision"],
                                           {**body, "project_ids": sorted([*body["project_ids"], project_id])}, "operator", conn=db)
