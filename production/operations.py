"""Local operator identity/envelope commands; never exposed by the author API.

Run on the separately controlled service host/account. POSIX modes do not isolate
an unrestricted agent running as this same OS user. No provider credential or
home-directory discovery occurs. Secrets go only to new private files. A crash
before SQLite commit can leave an unusable token file; it grants no authority.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote

from pydantic import Field, ValidationError, model_validator

from production.auth import SYSTEM_PROJECT, AuthService
from production.billing import bill, format_table
from production.cli import CLIError, Parser, read_json
from production.contracts import (
    BudgetKey,
    Contract,
    Digest,
    DomainError,
    Identifier,
    Mutation,
    ObjectRef,
    canonical_json,
    content_hash,
)
from production.jobs import MAX_POLLS
from production.record_payloads import RecordPayloads, local_record_payloads
from production.review_payloads import MARKER, DirectoryBlobs, ReviewPayloads, for_store
from production.store import MAX_AMOUNT, SCHEMA, SCHEMA_VERSION, Store

Amount = Annotated[int, Field(ge=0, le=MAX_AMOUNT)]
CredentialID = Annotated[str, Field(pattern=r'^credential_[a-f0-9]{32}$')]
OutputName = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,100}\.token$')]
ReleaseID = Annotated[str, Field(pattern=r'^release_[a-f0-9]{64}$')]
Reason = Annotated[str, Field(min_length=1, max_length=2000)]
# The manifest lists every record revision and file; the live store outgrew 4 MiB on 2026-09-24.
MAX_BACKUP_MANIFEST_BYTES = 64 * 1024 * 1024



class RetainedCostHold(Contract):
    """Explicit accounting liability, never permission for unknown execution."""
    project_id: Identifier
    reservation_id: Identifier
    budget_key: BudgetKey
    unit: Identifier
    amount: Amount
    target: ObjectRef



class RetainedObservation(Contract):
    """Quarantine completed local reading, without deciding its remote outcome."""
    project_id: Identifier
    job: ObjectRef
    observation: ObjectRef
    attempt: ObjectRef
    reason: Reason



class BackupRequest(Contract):
    destination: str
    media_root: str
    max_total_bytes: Annotated[int, Field(gt=0, le=2**40)] = 10*1024**3
    max_files: Annotated[int, Field(gt=0, le=100000)] = 10000


class RestoreRequest(Contract):
    source: str
    destination: str
    manifest_sha256: Digest
    max_total_bytes: Annotated[int, Field(gt=0, le=2**40)] = 10*1024**3
    max_files: Annotated[int, Field(gt=0, le=100000)] = 10000



def _private_parents(path: Path) -> None:
    missing=[]
    while not path.exists():
        missing.append(path)
        path=path.parent
    os.close(_directory(path))
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)


def _copy_file(source: Path, destination: Path, limit: int) -> dict[str, Any]:
    """Bounded private no-clobber copy; hash the actual copied bytes."""
    _absolute(source)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise DomainError('invalid_input', 'Backup source is nonregular or exceeds the byte bound')
        _private_parents(destination.parent)
        out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(out, 'wb') as target:
            while chunk := stream.read(1024*1024):
                size += len(chunk)
                if size > limit:
                    raise DomainError('invalid_input', 'Backup source grew beyond its byte bound')
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise DomainError('stale_input', 'Backup source changed during copying')
        return {'size':size,'sha256':digest.hexdigest()}


def _snapshot_inventory(path: Path, record_payloads: RecordPayloads | None = None) -> dict[str, Any]:
    """Read-only validation before constructing Store; never migrate an imported DB."""
    conn = sqlite3.connect('file:'+quote(str(path), safe='/')+'?mode=ro&immutable=1', uri=True)
    expected = sqlite3.connect(':memory:')
    codec = record_payloads or RecordPayloads(ReviewPayloads(DirectoryBlobs(path.parent/'review-payloads')))
    try:
        conn.row_factory = sqlite3.Row
        for statement in SCHEMA:
            expected.execute(statement)
        schema = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        if (conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                or [tuple(r) for r in conn.execute(schema)] != expected.execute(schema).fetchall()
                or conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                or conn.execute('PRAGMA foreign_key_check').fetchone()):
            raise ValueError('Incompatible or damaged schema')
        records: list[dict[str, Any]] = []
        media: dict[str, int] = {}
        media_refs: dict[str, dict[str, Any]] = {}
        releases: dict[str, Any] = {}
        review_payloads: dict[str, Any] = {}
        for row in conn.execute('SELECT r.*,o.kind FROM revisions r JOIN objects o USING(project_id,object_id) ORDER BY r.project_id,r.object_id,r.revision'):
            physical = json.loads(row['body'])
            body = codec.decode(physical, row['digest'])
            for external_ref in codec.references(physical, row['digest']):
                review_payloads[content_hash(external_ref)] = external_ref
            if content_hash(body) != row['digest']:
                raise ValueError('Revision fingerprint changed')
            records.append({k:row[k] for k in ('project_id','object_id','revision','digest','kind')})
            field = {'review-run': 'messages', 'review-turn': 'request'}.get(row['kind'])
            reference = body.get(field) if field else None
            if isinstance(reference, dict) and MARKER in reference:
                review_payloads[content_hash(reference)] = reference
            if row['kind'] == 'media':
                sha, size = body['sha256'], body['size']
                if (len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha) or type(size) is not int or size<=0
                        or sha in media and media[sha]!=size):
                    raise ValueError('Invalid media identity')
                media[sha]=size
                media_refs[sha] = {key: row[key] for key in ('project_id', 'object_id', 'revision')}
            if row['kind'] == 'release':
                if (row['project_id']!=SYSTEM_PROJECT or row['revision']!=1 or row['author']!='operator'
                        or row['object_id']!='release_'+content_hash(body)):
                    raise ValueError('Invalid release identity')
                # Historical release records stay in the store; their files are archived and
                # hash-checked so existing backups keep restoring, but their catalogs are no longer interpreted.
                for name, item in body['files'].items():
                    relative=Path(name)
                    value=base64.b64decode(item['bytes'],validate=True)
                    if (relative.is_absolute() or '..' in relative.parts or str(relative)!=name
                            or hashlib.sha256(value).hexdigest()!=item['sha256']):
                        raise ValueError('Invalid release file')
                releases[row['object_id']]=body['files']
        for row in conn.execute('SELECT result FROM idempotency'):
            value = json.loads(row['result'])
            if isinstance(value, dict) and set(value) == {'$mvgp_replay_v1'}:
                record = value['$mvgp_replay_v1']
                if not isinstance(record, dict) or set(record) != {'digest', 'body'}:
                    raise ValueError('Invalid replay envelope')
                logical = codec.decode(record['body'], record['digest'])
                if not isinstance(logical, dict) or set(logical) != {'result'}:
                    raise ValueError('Invalid replay result')
                for external_ref in codec.references(record['body'], record['digest']):
                    review_payloads[content_hash(external_ref)] = external_ref
        if conn.execute('SELECT 1 FROM objects o LEFT JOIN revisions r ON o.project_id=r.project_id AND o.object_id=r.object_id AND o.current_revision=r.revision WHERE r.object_id IS NULL LIMIT 1').fetchone():
            raise ValueError('Missing current revision')
        return {'records':records,'media':media,'media_refs':media_refs,'releases':releases,
                'review_payloads': list(review_payloads.values())}
    except (sqlite3.DatabaseError, ValueError, KeyError, TypeError):
        raise DomainError('release_mismatch','Backup database schema, history or embedded release integrity is invalid') from None
    finally:
        conn.close()
        expected.close()



def _verified_manifest_like(path: str, sha256: str) -> bytes:
    """An operator-supplied evidence file: absolute, regular, bounded and exactly the stated bytes."""
    file = _absolute(path)
    return _verified_manifest_file(file, sha256)


def _verified_manifest_file(file: Path, sha256: str) -> bytes:
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BACKUP_MANIFEST_BYTES:
                raise DomainError('invalid_input', 'Evidence file is nonregular or too large')
            data = stream.read(MAX_BACKUP_MANIFEST_BYTES + 1)
    except OSError:
        raise DomainError('invalid_input', 'Evidence file is missing or unreadable') from None
    if len(data) > MAX_BACKUP_MANIFEST_BYTES or hashlib.sha256(data).hexdigest() != sha256:
        raise DomainError('release_mismatch', 'Evidence file bytes differ from the stated hash')
    return data


def _verified_manifest(root: Path, sha256: str) -> bytes:
    """Read the backup manifest under its own bound; release sources keep the smaller file bound."""
    try:
        fd = os.open(root/'manifest.json', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BACKUP_MANIFEST_BYTES:
                raise DomainError('invalid_input', 'Backup manifest is nonregular or exceeds its byte bound')
            data = stream.read(MAX_BACKUP_MANIFEST_BYTES + 1)
    except OSError:
        raise DomainError('release_mismatch', 'Backup manifest is missing or unreadable') from None
    if len(data) > MAX_BACKUP_MANIFEST_BYTES:
        raise DomainError('invalid_input', 'Backup manifest exceeds its byte bound')
    if hashlib.sha256(data).hexdigest() != sha256:
        raise DomainError('release_mismatch', 'Operator source hash differs from the explicit manifest')
    return data


class OperatorConfig(Contract):
    database: str
    public_origin: str
    operator_id: Identifier
    credential_directory: str




class RetryResultDownloadRequest(Contract):
    project_id: Identifier
    job: ObjectRef
    reason: Reason



class IssueRequest(Contract):
    actor_id: Identifier
    role: Literal['agent', 'viewer', 'worker']  # no operator-issued human exchange
    project_ids: Annotated[list[Identifier], Field(max_length=256)]
    ttl_seconds: Annotated[int, Field(ge=1, le=90*86400)] = 300
    output_name: OutputName
    allow_create_project: bool = False
    # the worker and the film service may see every project (one owner).
    all_projects: bool = False

    @model_validator(mode='after')
    def scopes(self) -> Self:
        if (len(self.project_ids) != len(set(self.project_ids)) or SYSTEM_PROJECT in self.project_ids
                or (self.allow_create_project and self.role != 'agent')
                or (self.all_projects and (self.role not in ('worker', 'agent') or self.allow_create_project))
                or (not self.project_ids and not self.allow_create_project and not self.all_projects)):
            raise ValueError('Explicit limited scopes are required')
        return self


class RotateRequest(Contract):
    credential_id: CredentialID
    expected_revision: Annotated[int, Field(ge=1)]
    ttl_seconds: Annotated[int, Field(ge=1, le=90*86400)]
    output_name: OutputName


class RevokeRequest(Contract):
    credential_id: CredentialID
    expected_revision: Annotated[int, Field(ge=1)]


class EnvelopeRequest(Contract):
    project_id: Identifier
    budget_key: BudgetKey = 'legacy'
    expected_revision: Amount
    expected_ceiling: Amount | None
    limit: Amount
    unit: Identifier
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class ReconcileCostRequest(Mutation):
    project_id: Identifier
    reservation_id: Identifier
    target: ObjectRef
    expected_reserved_amount: Amount
    evidence_file: str
    evidence_sha256: Digest
    reason: Reason



class AbandonObservationRequest(Mutation):
    project_id: Identifier
    job: ObjectRef
    observation: ObjectRef
    attempt: ObjectRef
    acknowledge_unresolved_charge: bool = Field(strict=True)
    reason: Reason


class AbandonGenerationRequest(Mutation):
    project_id: Identifier
    job: ObjectRef
    acknowledge_unresolved_charge: bool = Field(strict=True)
    reason: Reason


# Job types whose lost answer cannot be looked up (apilio answers synchronously and lists nothing; a fal request is
# listed only after it ends and a completion has no prompt to match), so the operator closes them (live 2026-09-25).
UNLISTABLE_GENERATIONS = frozenset({'apilio_gpt_image_2_5', 'fal_seedance_2_5', 'fal_seedance_2_5_complete'})



class BillingAttestation(Contract):
    """Operator-normalized private ledger match, not a provider API response."""
    schema_version: Literal[1]
    project_id: Identifier
    reservation_id: Identifier
    target: ObjectRef
    budget_key: BudgetKey
    unit: Identifier
    # fal: the video provider since 2026-09-25; its usage export is the bill.
    provider: Literal['deepseek', 'apilio', 'higgsfield', 'fal']
    ledger_entry_id: Annotated[str, Field(min_length=1, max_length=256)]
    provider_request_id: Annotated[str, Field(min_length=1, max_length=256)]
    actual_amount: Amount
    outcome: Literal['billed', 'confirmed_no_charge']
    verification: Reason

    @model_validator(mode='after')
    def explicit_amount(self) -> Self:
        if (self.target.digest is None or not self.ledger_entry_id.strip()
                or not self.provider_request_id.strip()
                or (self.actual_amount == 0) != (self.outcome == 'confirmed_no_charge')):
            raise ValueError('Exact target and explicit supplier charge outcome required')
        return self


def _absolute(value: str | Path) -> Path:
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or str(path) != str(value)
            or any(part.is_symlink() for part in (path, *path.parents))):
        raise DomainError('forbidden', 'Operator paths must be explicit absolute paths without symlinks')
    return path


def _attestation(evidence_file: str, evidence_sha256: str, model: type[Contract]) -> Any:
    """Read one private, size-bounded, hash-pinned operator attestation without duplicate keys."""
    try:
        path = _absolute(evidence_file)
        os.close(_directory(path.parent))
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _private(info)
            if info.st_size > 262144:
                raise ValueError
            raw = stream.read(262145)
        if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != evidence_sha256:
            raise ValueError
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        return model.model_validate(json.loads(raw, object_pairs_hook=pairs))
    except (OSError, ValueError, ValidationError, RecursionError, DomainError):
        raise DomainError('invalid_input', 'Provider absence evidence is unavailable, unsafe, changed or invalid') from None


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    if (info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise DomainError('forbidden', 'Operator files and directories must be private and owned by this account')


def _directory(path: Path) -> int:
    _absolute(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _private(os.fstat(fd), directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def load_config(path: Path) -> OperatorConfig:
    """Explicit bounded private configuration; no environment or home fallback."""
    try:
        path = _absolute(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _private(info)
            if info.st_size > 65536:
                raise ValueError
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        return OperatorConfig.model_validate(json.loads(raw, object_pairs_hook=pairs))
    except (OSError, ValueError, ValidationError, RecursionError):
        raise DomainError('invalid_input', 'Operator configuration is unavailable, unsafe or invalid') from None


class Operations:
    """Trusted local entry point, not an HTTP service or an author capability."""
    def __init__(self, config: OperatorConfig) -> None:
        self.config = config
        self._auth: AuthService | None = None
        try:
            self.output_root = _absolute(config.credential_directory)
            os.close(_directory(self.output_root))
            database = _absolute(config.database)
            parent = _directory(database.parent)
            try:
                try:
                    fd = os.open(database.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                except FileExistsError:
                    fd = os.open(database.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    _private(os.fstat(fd))
                finally:
                    os.close(fd)
            finally:
                os.close(parent)
            self.store = Store(database)
            self.store.record_payloads = local_record_payloads(database)
            self._auth = AuthService(self.store, config.public_origin)
        except (OSError, ValueError):
            raise DomainError('invalid_input', 'Operator service configuration cannot be opened safely') from None

    @property
    def auth(self) -> AuthService:
        # Recovery commands must be reachable without AuthService's startup write.
        if self._auth is None:
            self._auth = AuthService(self.store, self.config.public_origin)
        return self._auth

    def close(self) -> None:
        return None


    def _audit(self, conn: sqlite3.Connection, kind: str, body: dict[str, Any], project: str = SYSTEM_PROJECT) -> None:
        self.store.append_event(project, kind, {'operator_id': self.config.operator_id, **body}, conn=conn)


    def _write_issue(self, request: IssueRequest, rotate: RotateRequest | None = None) -> dict[str, Any]:
        auth = self.auth
        parent = _directory(self.output_root)
        created: tuple[int, int] | None = None
        try:
            with self.store.transaction() as conn:
                if rotate:
                    old = self._credential(rotate.credential_id, rotate.expected_revision, conn)
                    if old['body']['revoked']:
                        raise DomainError('forbidden', 'Revoked credentials cannot be rotated')
                token = auth.provision_token(request.actor_id, request.role, request.project_ids,
                    request.ttl_seconds, allow_create_project=request.allow_create_project,
                    all_projects=request.all_projects, conn=conn)
                credential_id = token.split('.')[0]
                fd = os.open(request.output_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                info = os.fstat(fd)
                created = (info.st_dev, info.st_ino)
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write((token+'\n').encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(parent)
                if rotate:
                    auth.revoke(rotate.credential_id, conn=conn)
                self._audit(conn, 'operator.credential.rotated' if rotate else 'operator.credential.issued',
                    {'credential_id': credential_id, 'role': request.role, 'actor_id': request.actor_id,
                     'project_ids': request.project_ids, 'all_projects': request.all_projects,
                     'previous_id': rotate.credential_id if rotate else None})
            return {'credential_id': credential_id, 'revision': 1, 'role': request.role,
                    'token_file': str(self.output_root/request.output_name)}
        except BaseException as exc:
            if created:
                try:
                    info = os.stat(request.output_name, dir_fd=parent, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == created:
                        os.unlink(request.output_name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            if isinstance(exc, OSError):
                raise DomainError('invalid_input', 'Credential output failed or destination already exists; issuance was rolled back') from None
            raise
        finally:
            os.close(parent)

    def issue(self, request: IssueRequest) -> dict[str, Any]:
        return self._write_issue(request)

    def _credential(self, credential_id: str, expected_revision: int, conn: sqlite3.Connection) -> dict[str, Any]:
        RevokeRequest(credential_id=credential_id, expected_revision=expected_revision)
        record = self.store.get_object(SYSTEM_PROJECT, credential_id, conn=conn)
        if record['revision'] != expected_revision:
            raise DomainError('revision_conflict', 'Credential changed; read the current operator record before retrying')
        return record

    def rotate(self, request: RotateRequest) -> dict[str, Any]:
        with self.store.transaction(write=False) as conn:
            old = self._credential(request.credential_id, request.expected_revision, conn)
            body = old['body']
            if old['kind'] != 'credential' or body['role'] not in ('agent', 'viewer', 'worker'):
                raise DomainError('forbidden', 'Only agent, viewer and worker credentials can be operator-rotated')
            issue = IssueRequest(actor_id=body['actor_id'], role=body['role'],
                project_ids=body['project_ids'], ttl_seconds=request.ttl_seconds, output_name=request.output_name,
                allow_create_project=body.get('allow_create_project', False),
                all_projects=body.get('all_projects', False) is True)
        return self._write_issue(issue, request)

    def revoke(self, credential_id: str, expected_revision: int) -> dict[str, Any]:
        auth = self.auth
        with self.store.transaction() as conn:
            self._credential(credential_id, expected_revision, conn)
            auth.revoke(credential_id, conn=conn)
            self._audit(conn, 'operator.credential.revoked', {'credential_id': credential_id})
            record = self.store.get_object(SYSTEM_PROJECT, credential_id, conn=conn)
            return {'credential_id': credential_id, 'revision': record['revision'], 'revoked': True}

    def envelope(self, project_id: str, *, budget_key: str = 'legacy',
                 conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Revision is latest budget-change event sequence, including trusted human changes."""
        if project_id == SYSTEM_PROJECT:
            raise DomainError('forbidden', 'Internal project cannot receive a production budget')
        with self.store._using(conn, write=False) as db:
            self.store.get_object(project_id, project_id, conn=db)
            revision = db.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE project_id=? "
                "AND kind IN ('budget.changed','operator.envelope.changed') "
                "AND COALESCE(json_extract(body,'$.budget_key'),'legacy')=?", (project_id,budget_key)).fetchone()[0]
            try:
                current = self.store.budget(project_id, budget_key=budget_key, conn=db)
            except DomainError as exc:
                if exc.code != 'missing_prerequisite':
                    raise
                current = {'project_id': project_id, 'budget_key': budget_key, 'ceiling': None, 'unit': None, 'spent': 0, 'reserved': 0}
            return {**current, 'revision': revision}

    def set_envelope(self, request: EnvelopeRequest) -> dict[str, Any]:
        with self.store.transaction() as conn:
            previous = self.envelope(request.project_id, budget_key=request.budget_key, conn=conn)
            if previous['revision'] != request.expected_revision or previous['ceiling'] != request.expected_ceiling:
                raise DomainError('revision_conflict', 'Spending envelope changed; inspect its current revision and ceiling')
            self.store.set_budget(request.project_id, request.limit, request.unit, budget_key=request.budget_key, conn=conn)
            self._audit(conn, 'operator.envelope.changed', {'previous': previous, 'limit': request.limit,
                'budget_key': request.budget_key, 'unit': request.unit, 'reason': request.reason}, request.project_id)
            return self.envelope(request.project_id, budget_key=request.budget_key, conn=conn)

    def retry_result_download(self, request: RetryResultDownloadRequest) -> dict[str, Any]:
        """Reset only a recorded result's download allowance; never change billing."""
        with self.store.transaction() as db:
            if request.project_id == SYSTEM_PROJECT or request.job.digest is None:
                raise DomainError('invalid_input', 'An exact project job reference is required')
            current = self.store.get_object(request.project_id, request.job.object_id, conn=db)
            if current['revision'] != request.job.revision or current['digest'] != request.job.digest:
                raise DomainError('revision_conflict', 'Generation changed after inspection')
            body = current['body']
            if (current['kind'] != 'job' or current['author'] not in ('submission_service', 'worker_service')
                    or body.get('state') != 'unknown' or body.get('lease') is not None
                    or not body.get('pending_result') or body.get('last_error') not in ('invalid_media', 'provider_failure')):
                raise DomainError('forbidden', 'Only an inactive unknown job with a failed recorded download may be retried')
            try:
                reference = ObjectRef.model_validate(body['pending_result'])
            except ValidationError:
                raise DomainError('forbidden', 'An exact worker result receipt is required') from None
            receipt = self.store.get_object(request.project_id, reference.object_id, revision=reference.revision, conn=db)
            if (receipt['digest'] != reference.digest or receipt['kind'] != 'provider-receipt'
                    or receipt['author'] != 'worker_service' or not receipt['body'].get('download_reference')):
                raise DomainError('forbidden', 'An exact worker receipt with a download reference is required')
            updated = self.store.append_revision(request.project_id, current['object_id'], current['revision'],
                {**body, 'download_count': 0, 'last_error': None}, current['author'], conn=db)
            result = {key: updated[key] for key in ('object_id', 'revision', 'digest')}
            self._audit(db, 'operator.result_download.retried', {'job': result,
                'previous_download_count': body.get('download_count', 0), 'previous_last_error': body['last_error'],
                'reason': request.reason}, request.project_id)
            return {'job': result, 'state': 'unknown', 'billing_changed': False}


    def abandon_observation(self, request: AbandonObservationRequest) -> dict[str, Any]:
        """Stop waiting for a lost read-only response; retain evidence and its bill."""
        if request.project_id == SYSTEM_PROJECT or not request.acknowledge_unresolved_charge:
            raise DomainError('invalid_input', 'Explicit unresolved observation charge acknowledgment required')

        def abandon(db: sqlite3.Connection) -> dict[str, Any]:
            job = self.store.get_object(request.project_id, request.job.object_id, conn=db)
            body = job['body']
            reservation = db.execute('SELECT * FROM reservations WHERE project_id=? AND reservation_id=?',
                (request.project_id, body.get('reservation_id'))).fetchone()
            if not reservation or reservation['state'] != 'unknown':
                raise DomainError('revision_conflict', 'The unresolved observation liability must remain recorded')
            budget = self.store.budget(request.project_id, budget_key=reservation['budget_key'], conn=db)
            hold = RetainedCostHold(project_id=request.project_id, reservation_id=reservation['reservation_id'],
                budget_key=reservation['budget_key'], unit=budget['unit'], amount=reservation['amount'],
                target=body['intent'])
            if reservation['object_id'] != hold.target.object_id:
                raise DomainError('revision_conflict', 'Observation reservation belongs to another intent')
            retained = RetainedObservation(project_id=request.project_id, job=request.job,
                observation=request.observation, attempt=request.attempt, reason=request.reason)
            self._retained_observations(db, [retained], [hold])
            evidence = self.store.create_object(SYSTEM_PROJECT, 'observation-abandonment', {
                **retained.model_dump(), 'operator_id': self.config.operator_id,
                'provider_outcome': 'unknown', 'billing_changed': False, 'approval_issued': False,
                'authority': 'operator-local-abandonment; not provider outcome reconciliation'}, 'operator', conn=db)
            marker = {key: evidence[key] for key in ('object_id', 'revision', 'digest')}
            updated = self.store.append_revision(request.project_id, job['object_id'], job['revision'],
                {**body, 'state': 'cancelled', 'provider_outcome': 'unknown', 'abandonment': marker},
                job['author'], conn=db)
            result = {'job': {key: updated[key] for key in ('object_id', 'revision', 'digest')},
                'state': 'cancelled', 'provider_outcome': 'unknown', 'billing_changed': False,
                'approval_issued': False, 'evidence': marker}
            self._audit(db, 'operator.observation.abandoned', result, request.project_id)
            return result

        return self.store.run_idempotent('operator.abandon-observation', request.idempotency_key,
            request.model_dump(), abandon)

    def abandon_generation(self, request: AbandonGenerationRequest) -> dict[str, Any]:
        """Close a paid generation whose answer was lost and cannot be looked up: the job is cancelled, its possible
        charge stays recorded as an unknown hold, and nothing is ever resent. A job with a provider request id is
        closed here only once the worker has stopped polling it (its polls are used up)."""
        if request.project_id == SYSTEM_PROJECT or not request.acknowledge_unresolved_charge or request.job.digest is None:
            raise DomainError('invalid_input', 'Exact job and unresolved-charge acknowledgment required')

        def abandon(db: sqlite3.Connection) -> dict[str, Any]:
            job = self.store.get_object(request.project_id, request.job.object_id, conn=db)
            if job['revision'] != request.job.revision or job['digest'] != request.job.digest:
                raise DomainError('revision_conflict', 'Generation changed after inspection')
            body = job['body']
            intent = self.store.get_object(request.project_id, (body.get('intent') or {}).get('object_id', ''), conn=db)
            ib = intent['body']
            polls_used_up = bool(body.get('remote_job_id')) and body.get('poll_count', 0) >= MAX_POLLS
            if (job['kind'] != 'job' or job['author'] != 'worker_service' or body.get('state') != 'unknown'
                    or body.get('lease') is not None or (body.get('remote_job_id') and not polls_used_up)
                    or body.get('pending_result') or body.get('result')
                    or intent['kind'] != 'dispatch-intent' or intent['author'] != 'submission_service'
                    or ib.get('operation') not in ('submit', 'complete-draft')
                    or (ib.get('request') or {}).get('job_type') not in UNLISTABLE_GENERATIONS):
                raise DomainError('forbidden', 'Only an inactive unknown generation that cannot be looked up may be closed')
            reservation = db.execute('SELECT * FROM reservations WHERE project_id=? AND reservation_id=?',
                                     (request.project_id, body.get('reservation_id'))).fetchone()
            if not reservation or reservation['state'] != 'unknown' or reservation['object_id'] != intent['object_id']:
                raise DomainError('revision_conflict', 'The unresolved generation charge must stay recorded')
            evidence = self.store.create_object(SYSTEM_PROJECT, 'generation-abandonment', {
                'project_id': request.project_id, 'job': request.job.model_dump(),
                'intent': {key: intent[key] for key in ('object_id', 'revision', 'digest')},
                'job_type': ib['request']['job_type'], 'reservation_id': reservation['reservation_id'], 'reason': request.reason,
                'operator_id': self.config.operator_id, 'provider_outcome': 'unknown', 'billing_changed': False,
                'authority': 'operator-local-abandonment; the provider was not asked and nothing is resent'}, 'operator', conn=db)
            marker = {key: evidence[key] for key in ('object_id', 'revision', 'digest')}
            updated = self.store.append_revision(request.project_id, job['object_id'], job['revision'],
                {**body, 'state': 'cancelled', 'provider_outcome': 'unknown', 'abandonment': marker}, job['author'], conn=db)
            result = {'job': {key: updated[key] for key in ('object_id', 'revision', 'digest')}, 'state': 'cancelled',
                      'provider_outcome': 'unknown', 'billing_changed': False, 'evidence': marker}
            self._audit(db, 'operator.generation.abandoned', result, request.project_id)
            return result

        return self.store.run_idempotent('operator.abandon-generation', request.idempotency_key, request.model_dump(), abandon)


    def reconcile_cost(self, request: ReconcileCostRequest) -> dict[str, Any]:
        """Settle an unknown bill only; never repair/retry/approve a model task.

        The operator independently matches the private supplier ledger. A schema
        and hash validate that attestation's integrity, not its factual truth.
        The production agent has no HTTP route or credential for this command.
        """
        if request.project_id == SYSTEM_PROJECT or request.target.digest is None:
            raise DomainError('invalid_input', 'An exact production reservation owner is required')
        try:
            path = _absolute(request.evidence_file)
            os.close(_directory(path.parent))
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                _private(info)
                if info.st_size > 65536:
                    raise ValueError
                raw = stream.read(65537)
            if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != request.evidence_sha256:
                raise ValueError
            def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in items:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            proof = BillingAttestation.model_validate(json.loads(raw, object_pairs_hook=pairs))
        except (OSError, ValueError, ValidationError, RecursionError):
            raise DomainError('invalid_input', 'Billing evidence is unavailable, unsafe, changed or invalid') from None
        if (proof.project_id != request.project_id or proof.reservation_id != request.reservation_id
                or proof.target != request.target):
            raise DomainError('release_mismatch', 'Billing evidence names another reservation or target')

        def settle(db: sqlite3.Connection) -> dict[str, Any]:
            target = self.store.get_object(request.project_id, request.target.object_id, conn=db)
            if (target['revision'] != request.target.revision or target['digest'] != request.target.digest):
                raise DomainError('revision_conflict', 'Reservation owner changed; inspect it before reconciliation')
            if (target['kind'], target['author']) not in {
                    ('review-task', 'review_service'), ('dispatch-intent', 'submission_service')}:
                raise DomainError('forbidden', 'Only service-issued reservation owners may be reconciled')
            if target['kind'] == 'review-task':
                if target['body'].get('state') not in ('completed', 'failed', 'cancelled'):
                    raise DomainError('locked', 'Resolve the review outcome before reconciling its bill')
            else:
                jobs = [job for job in self.store.list_objects(request.project_id, kind='job', conn=db)
                    if job['body'].get('intent', {}).get('object_id') == target['object_id']]
                if (len(jobs) != 1 or jobs[0]['author'] not in ('submission_service', 'worker_service')
                        or jobs[0]['body'].get('state') not in ('succeeded', 'failed', 'cancelled')):
                    raise DomainError('locked', 'Resolve the generation or observation outcome before reconciling its bill')
            row = db.execute('SELECT * FROM reservations WHERE project_id=? AND reservation_id=?',
                (request.project_id, request.reservation_id)).fetchone()
            if (not row or row['state'] != 'unknown' or row['object_id'] != target['object_id']
                    or row['amount'] != request.expected_reserved_amount or row['budget_key'] != proof.budget_key):
                raise DomainError('revision_conflict', 'Reservation is not the exact unknown hold inspected by the operator')
            budget = self.store.budget(request.project_id, budget_key=proof.budget_key, conn=db)
            if budget['unit'] != proof.unit:
                raise DomainError('invalid_input', 'Billing units differ from the reserved provider account')
            entry = 'billing_'+content_hash({'provider':proof.provider, 'budget_key':proof.budget_key,
                'ledger_entry_id':proof.ledger_entry_id})
            try:
                self.store.get_object(SYSTEM_PROJECT, entry, conn=db)
            except DomainError as exc:
                if exc.code != 'not_found':
                    raise
            else:
                raise DomainError('idempotency_conflict', 'Supplier ledger entry is already assigned to a reservation')
            record = self.store.create_object(SYSTEM_PROJECT, 'billing-reconciliation', {
                'attestation':proof.model_dump(), 'evidence_sha256':request.evidence_sha256,
                'operator_id':self.config.operator_id, 'reason':request.reason,
                'authority':'operator-ledger-attestation; not independently verified by software'},
                'operator', object_id=entry, conn=db)
            budget = self.store.settle(request.project_id, request.reservation_id, proof.actual_amount, conn=db)
            body = {'reservation_id':request.reservation_id, 'target':request.target.model_dump(),
                'budget_key':proof.budget_key, 'unit':proof.unit, 'actual_amount':proof.actual_amount,
                'evidence_sha256':request.evidence_sha256, 'reconciliation_id':record['object_id'],
                'task_outcome_changed':False}
            self._audit(db, 'operator.cost.reconciled', body, request.project_id)
            return {**body, 'budget':budget}

        return self.store.run_idempotent('operator:cost:'+self.config.operator_id+':'+request.project_id,
            request.idempotency_key, request.model_dump(), settle)



    def _retained_observations(self, conn: sqlite3.Connection, retained: list[RetainedObservation],
                               costs: list[RetainedCostHold]) -> set[tuple[str, str]]:
        allowed: set[tuple[str, str]] = set()
        for item in retained:
            key = (item.project_id, item.job.object_id)
            if key in allowed:
                raise DomainError('invalid_input', 'Duplicate retained observation')
            records = []
            for reference, kind, author in ((item.job, 'job', 'worker_service'),
                    (item.observation, 'observation', 'reader_service'),
                    (item.attempt, 'provider-attempt', 'worker_service')):
                record = self.store.get_object(item.project_id, reference.object_id, conn=conn)
                if (reference.digest is None or record['revision'] != reference.revision
                        or record['digest'] != reference.digest or record['kind'] != kind or record['author'] != author
                        or (kind != 'job' and record['revision'] != 1)):
                    raise DomainError('revision_conflict', 'Retained observation requires exact current service records')
                records.append(record)
            job, observation, attempt = (record['body'] for record in records)
            intent_ref = job.get('intent', {})
            intent = self.store.get_object(item.project_id, intent_ref.get('object_id', ''), conn=conn)
            if (intent['kind'] != 'dispatch-intent' or intent['author'] != 'submission_service'
                    or intent['revision'] != 1
                    or intent_ref != {k: intent[k] for k in ('object_id', 'revision', 'digest')}
                    or intent['body'].get('operation') != 'observe'
                    or job.get('state') != 'unknown' or 'lease' not in job or job['lease'] is not None
                    or job.get('remote_job_id') or job.get('pending_result')
                    or job.get('observation') != item.observation.model_dump()
                    or job.get('result') != item.observation.model_dump()
                    or job.get('attempt_id') != item.attempt.object_id
                    or attempt.get('job_id') != item.job.object_id or attempt.get('intent') != intent_ref
                    or attempt.get('request') != intent['body'].get('request')
                    or observation.get('intent') != intent_ref or observation.get('status') != 'unknown'
                    or observation.get('failure') != 'transport_outcome_unknown'
                    or observation.get('response_journal') or observation.get('http_status')
                    or observation.get('raw_response') or observation.get('actual_billed_cost') is not None
                    or observation.get('reservation_action') != 'hold'
                    or observation.get('authoritative_review') is not False):
                raise DomainError('locked', 'Only completed local observations with unknown transport may be retained')
            source = intent['body'].get('request', {}).get('media', {})
            if (not source.get('sha256') or observation.get('source') != source.get('object_ref')
                    or observation.get('source_sha256') != source['sha256']
                    or not any(cost.project_id == item.project_id
                        and cost.reservation_id == job.get('reservation_id')
                        and cost.target.model_dump() == intent_ref for cost in costs)):
                raise DomainError('unknown_outcome', 'Retained observation needs exact source and retained cost binding')
            allowed.add(key)
        return allowed


    def backup(self, request: BackupRequest) -> dict[str, Any]:
        """PRIVATE exact snapshot; never a public export."""
        destination, media_root = _absolute(request.destination), _absolute(request.media_root)
        destination.mkdir(mode=0o700)
        database = destination/'metadata.sqlite'
        if self.store.path.stat().st_size > request.max_total_bytes:
            raise DomainError('invalid_input', 'Database exceeds backup byte budget')
        self.store.backup(database)
        database.chmod(0o600)
        inventory = _snapshot_inventory(database, self.store.record_payloads)
        total = database.stat().st_size
        if total > request.max_total_bytes:
            raise DomainError('invalid_input', 'Database snapshot exceeds backup byte budget')
        with database.open('rb') as stream:
            database_hash=hashlib.file_digest(stream,'sha256').hexdigest()
        files = {'metadata.sqlite': {'sha256':database_hash,'size':total}}
        payloads = for_store(self.store)
        for reference in inventory['review_payloads']:
            for digest, raw in payloads.blobs(reference):
                name = 'review-payloads/' + digest
                if name in files:
                    continue
                if total + len(raw) > request.max_total_bytes or len(files) >= request.max_files:
                    raise DomainError('invalid_input', 'Private review payload exceeds backup capacity')
                target = destination/name
                _private_parents(target.parent)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                files[name] = {'sha256': digest, 'size': len(raw)}
                total += len(raw)
        for sha, size in inventory['media'].items():
            if len(files)>=request.max_files:
                raise DomainError('invalid_input','Backup exceeds finite file bound')
            path = f'media/objects/{sha[:2]}/{sha}'
            source = media_root/'objects'/sha[:2]/sha
            entry = _copy_file(source,destination/path,request.max_total_bytes-total)
            if entry != {'sha256':sha,'size':size}:
                raise DomainError('invalid_media','Backup media bytes differ from snapshot references')
            files[path], total = entry, total+size
        for rid, entries in inventory['releases'].items():
            for relative, entry in entries.items():
                raw=base64.b64decode(entry['bytes'],validate=True)
                total+=len(raw)
                if total>request.max_total_bytes or len(files)>=request.max_files:
                    raise DomainError('invalid_input','Backup exceeds finite file or byte bound')
                name=f'releases/{rid}/{relative}'
                target=destination/name
                _private_parents(target.parent)
                fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                files[name]={'sha256':entry['sha256'],'size':len(raw)}
        if total>request.max_total_bytes or len(files)>request.max_files:
            raise DomainError('invalid_input','Backup exceeds finite file or byte bound')
        manifest={'schema':'mvgp-private-backup-v1','database_schema':SCHEMA_VERSION,'files':files,
            'records':inventory['records'],'releases':sorted(inventory['releases']),
            'classification':'PRIVATE: contains credentials and signed URLs; not a public export.',
            'media_bytes_included':True, 'review_payloads_included':True,
            'restore_policy':'Isolated restore only; revoke credentials, disable activation, explicitly reissue and switch over.'}
        raw=canonical_json(manifest).encode()
        if len(raw)>MAX_BACKUP_MANIFEST_BYTES:
            raise DomainError('invalid_input','Backup manifest exceeds its byte bound')
        fd=os.open(destination/'manifest.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return {'destination':str(destination),'manifest_sha256':hashlib.sha256(raw).hexdigest(),
                'files':len(files),'bytes':total,'private':True,'media_bytes_included':True}

    def restore(self, request: RestoreRequest) -> dict[str, Any]:
        """Never write over a live database; historical records retain original hashes."""
        source,destination=_absolute(request.source),_absolute(request.destination)
        os.close(_directory(source))
        raw=_verified_manifest(source,request.manifest_sha256)
        try:
            manifest=json.loads(raw)
            files=manifest['files']
            if (manifest['schema']!='mvgp-private-backup-v1' or manifest['database_schema']!=SCHEMA_VERSION
                    or manifest['media_bytes_included'] is not True or not isinstance(files,dict)
                    or not files or len(files)>request.max_files
                    or any(type(v['size']) is not int or v['size']<0 for v in files.values())
                    or sum(v['size'] for v in files.values())>request.max_total_bytes):
                raise ValueError
            for name in files:
                relative=Path(name)
                if relative.is_absolute() or '..' in relative.parts or str(relative)!=name or name in ('','.','manifest.json'):
                    raise ValueError
                target=_absolute(source/relative)
                _private(target.stat())
            _private((source/'manifest.json').stat())
        except (KeyError,TypeError,ValueError,OSError):
            raise DomainError('invalid_input','Private backup manifest or file permissions are invalid') from None
        destination.mkdir(mode=0o700)
        if 'metadata.sqlite' not in files:
            raise DomainError('release_mismatch','Backup has no exact metadata snapshot')
        for name in sorted(files,key=lambda name:(name!='metadata.sqlite',name)):
            expected=files[name]
            actual=_copy_file(source/name,destination/name,expected['size'])
            if actual!=expected:
                raise DomainError('release_mismatch','Restore file hash or size differs from trusted backup manifest')
        inventory=_snapshot_inventory(destination/'metadata.sqlite')
        required={'metadata.sqlite'}|{f'media/objects/{sha[:2]}/{sha}' for sha in inventory['media']}
        required|={f'releases/{rid}/{name}' for rid, entries in inventory['releases'].items() for name in entries}
        payloads = ReviewPayloads(DirectoryBlobs(destination/'review-payloads'))
        for reference in inventory['review_payloads']:
            for digest, raw in payloads.blobs(reference):
                name = 'review-payloads/' + digest
                required.add(name)
                if files.get(name) != {'sha256': digest, 'size': len(raw)}:
                    raise DomainError('release_mismatch', 'Restored review payload differs from immutable metadata')
        if (set(files)!=required or manifest['records']!=inventory['records']
                or manifest['releases']!=sorted(inventory['releases'])):
            raise DomainError('release_mismatch','Restore manifest omits or adds database-bound objects or releases')
        for sha,size in inventory['media'].items():
            if files[f'media/objects/{sha[:2]}/{sha}']!={'sha256':sha,'size':size}:
                raise DomainError('release_mismatch','Restored media disagrees with exact database references')
        for rid,entries in inventory['releases'].items():
            for name,entry in entries.items():
                if files[f'releases/{rid}/{name}']['sha256']!=entry['sha256']:
                    raise DomainError('release_mismatch','Restored executable archive disagrees with immutable release')
        restored=Store(destination/'metadata.sqlite')
        restored.record_payloads = RecordPayloads(payloads)
        auth=AuthService(restored,self.config.public_origin)
        revoked=0
        with restored.transaction() as conn:
            for credential in restored.list_objects(SYSTEM_PROJECT,conn=conn):
                if credential['kind'] in ('credential','human-exchange','human-session','viewer-session'):
                    auth.revoke(credential['object_id'],conn=conn)
                    revoked+=1
            restored.append_event(SYSTEM_PROJECT,'operator.backup.restored',{'operator_id':self.config.operator_id,
                'manifest_sha256':request.manifest_sha256,'credentials_revoked':revoked,'active':False},conn=conn)
        return {'destination':str(destination),'database':str(destination/'metadata.sqlite'),
            'media_root':str(destination/'media'),'release_archives':str(destination/'releases'),
            'credentials_revoked':revoked,'active':False,'switch_over_performed':False,
            'next_steps':['Verify isolated deployment and access.',
                          'Reconcile external provider activity since the snapshot, including calls absent from its journal; never blindly retry.',
                          'Resolve unknown work; explicitly activate the verified runtime.',
                          'Issue fresh scoped credentials and explicitly switch the service configuration.']}


def main(argv: Sequence[str] | None = None) -> int:
    """Operator commands: credentials, envelopes, bills, settling unknown costs, backups."""
    parser = Parser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    handlers = {
        'issue': lambda o, body: o.issue(IssueRequest.model_validate(body)),
        'rotate': lambda o, body: o.rotate(RotateRequest.model_validate(body)),
        'revoke': lambda o, body: o.revoke(**RevokeRequest.model_validate(body).model_dump()),
        'set-envelope': lambda o, body: o.set_envelope(EnvelopeRequest.model_validate(body)),
        'reconcile-cost': lambda o, body: o.reconcile_cost(ReconcileCostRequest.model_validate(body)),
        'abandon-observation': lambda o, body: o.abandon_observation(AbandonObservationRequest.model_validate(body)),
        'abandon-generation': lambda o, body: o.abandon_generation(AbandonGenerationRequest.model_validate(body)),
        'retry-result-download': lambda o, body: o.retry_result_download(RetryResultDownloadRequest.model_validate(body)),
        'backup': lambda o, body: o.backup(BackupRequest.model_validate(body)),
        'restore': lambda o, body: o.restore(RestoreRequest.model_validate(body)),
    }
    for name in handlers:
        commands.add_parser(name).add_argument('--input', required=True)
    envelope = commands.add_parser('envelope')
    envelope.add_argument('project_id')
    envelope.add_argument('--budget-key', default='legacy')
    billing = commands.add_parser('bill')
    billing.add_argument('--project', required=True)
    billing.add_argument('--format', choices=('json', 'table'), default='json')
    operations = None
    try:
        args = parser.parse_args(argv)
        config = load_config(args.config)
        operations = Operations(config)
        if args.command == 'bill':
            result = bill(operations.store, args.project)
        elif args.command == 'envelope':
            result = operations.envelope(args.project_id, budget_key=args.budget_key)
        else:
            result = handlers[args.command](operations, read_json(args.input, sys.stdin))
        print(format_table(result) if args.command == 'bill' and args.format == 'table' else canonical_json(result))
        return 0
    except DomainError as exc:
        print(canonical_json(exc.as_dict()), file=sys.stderr)
        return 2
    except (OSError, ValueError, CLIError):
        print(canonical_json({'code': 'invalid_input', 'message': 'Invalid operator request or private configuration'}), file=sys.stderr)
        return 2
    finally:
        if operations is not None:
            operations.close()


if __name__ == '__main__':
    raise SystemExit(main())
