"""Single-host SQLite metadata store; domain services own authorization.

Every public mutation accepts ``conn=`` to participate in a caller's short atomic
transaction. Do not perform network I/O in transaction callbacks. Bodies are JSON
content, never executable migrations, authorization rules or SQL identifiers.
"""
from __future__ import annotations

import json
import marshal
import os
import re
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from production.contracts import DomainError, canonical_json, content_hash, new_id

if TYPE_CHECKING:
    from production.record_payloads import RecordPayloads

SCHEMA_VERSION = 2
MAX_AMOUNT = 2**63 - 1
# Optimization budgets, never limits on readable or accepted artifact content.
READ_BODY_CACHE_BYTES = 64 * 1024 * 1024
READ_BODY_CACHE_ENTRIES = 1024
SHARED_BODY_CACHE_BYTES = 512 * 1024 * 1024
SHARED_BODY_CACHE_ENTRIES = 50000


@dataclass
class _SharedBodies:
    entries: OrderedDict[tuple[str, str, int, str], bytes] = field(default_factory=OrderedDict)
    encoded_bytes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def decode(self, key: tuple[str, str, int, str], physical: Any,
               expand: Callable[[Any, str], Any]) -> Any:
        with self.lock:
            cached = self.entries.get(key)
            if cached is not None:
                self.entries.move_to_end(key)
        if cached is not None:
            # Only process-local bytes from decoded revision bodies, never storage.
            return marshal.loads(cached)
        value = expand(physical, key[3])
        serialized = marshal.dumps(value)
        size = len(serialized)
        if size > SHARED_BODY_CACHE_BYTES or SHARED_BODY_CACHE_ENTRIES <= 0:
            return value
        with self.lock:
            # Concurrent misses may decode the same immutable revision. Count it
            # once, without holding the lock across blob I/O or serialization.
            if key in self.entries:
                self.entries.move_to_end(key)
            else:
                while self.entries and (len(self.entries) >= SHARED_BODY_CACHE_ENTRIES
                                       or self.encoded_bytes + size > SHARED_BODY_CACHE_BYTES):
                    _, previous = self.entries.popitem(last=False)
                    self.encoded_bytes -= len(previous)
                self.entries[key] = serialized
                self.encoded_bytes += size
        return value


@dataclass
class _ReadBodies:
    connection: sqlite3.Connection
    entries: OrderedDict[tuple[str, str, int, str], tuple[bytes, int]] = field(default_factory=OrderedDict)
    encoded_bytes: int = 0

    def decode(self, key: tuple[str, str, int, str], encoded: str,
               expand: Callable[[Any, str], Any]) -> Any:
        cached = self.entries.pop(key, None)
        if cached is not None:
            self.entries[key] = cached
            # Exact immutable revision in this readonly SQLite snapshot. These
            # bytes are produced only from verified expanded JSON below, never
            # read from storage, provider data or a caller-supplied buffer.
            return marshal.loads(cached[0])
        value = expand(json.loads(encoded), key[3])
        encoded_size = len(encoded.encode('utf-8'))
        if encoded_size > READ_BODY_CACHE_BYTES or READ_BODY_CACHE_ENTRIES <= 0:
            return value
        serialized = marshal.dumps(value)
        size = encoded_size + len(serialized)
        if size <= READ_BODY_CACHE_BYTES:
            while self.entries and (len(self.entries) >= READ_BODY_CACHE_ENTRIES
                                   or self.encoded_bytes + size > READ_BODY_CACHE_BYTES):
                _, previous = self.entries.popitem(last=False)
                self.encoded_bytes -= previous[1]
            self.entries[key] = (serialized, size)
            self.encoded_bytes += size
            return value
        return value


SCHEMA = [
    "CREATE TABLE projects(project_id TEXT PRIMARY KEY)",
    """CREATE TABLE objects(project_id TEXT NOT NULL REFERENCES projects(project_id),
       object_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, current_revision INTEGER NOT NULL CHECK(current_revision>0),
       PRIMARY KEY(project_id,object_id))""",
    """CREATE TABLE revisions(project_id TEXT NOT NULL, object_id TEXT NOT NULL,
       revision INTEGER NOT NULL CHECK(revision>0), body TEXT NOT NULL CHECK(json_valid(body)),
       digest TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
       PRIMARY KEY(project_id,object_id,revision), FOREIGN KEY(project_id,object_id) REFERENCES objects(project_id,object_id))""",
    """CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
       project_id TEXT NOT NULL REFERENCES projects(project_id), kind TEXT NOT NULL,
       body TEXT NOT NULL CHECK(json_valid(body)), created_at TEXT NOT NULL DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
    """CREATE TABLE idempotency(scope TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
       result TEXT NOT NULL CHECK(json_valid(result)), PRIMARY KEY(scope,key))""",
    """CREATE TABLE budgets(project_id TEXT NOT NULL REFERENCES projects(project_id),
       budget_key TEXT NOT NULL,
       ceiling INTEGER NOT NULL CHECK(typeof(ceiling)='integer' AND ceiling>=0), unit TEXT NOT NULL,
       reserved INTEGER NOT NULL DEFAULT 0 CHECK(typeof(reserved)='integer' AND reserved>=0),
       spent INTEGER NOT NULL DEFAULT 0 CHECK(typeof(spent)='integer' AND spent>=0),
       PRIMARY KEY(project_id,budget_key))""",
    """CREATE TABLE reservations(project_id TEXT NOT NULL,
       reservation_id TEXT NOT NULL, budget_key TEXT NOT NULL, object_id TEXT,
       amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount>=0),
       actual INTEGER CHECK(actual IS NULL OR (typeof(actual)='integer' AND actual>=0)),
       state TEXT NOT NULL CHECK(state IN ('held','unknown','settled')),
       PRIMARY KEY(project_id,reservation_id),
       FOREIGN KEY(project_id,budget_key) REFERENCES budgets(project_id,budget_key),
       FOREIGN KEY(project_id,object_id) REFERENCES objects(project_id,object_id))""",
    "CREATE INDEX object_kind ON objects(project_id,kind)",
    "CREATE INDEX project_events ON events(project_id,sequence)",
]
for _table in ("revisions", "events", "idempotency"):
    for _operation in ("UPDATE", "DELETE"):
        SCHEMA.append(f"CREATE TRIGGER immutable_{_table}_{_operation.lower()} BEFORE {_operation} ON {_table} BEGIN SELECT RAISE(ABORT,'append-only record'); END")


def _amount(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_AMOUNT:
        raise DomainError("invalid_input", "Amount must be a nonnegative 64-bit integer")


def _budget_key(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise DomainError("invalid_input", "Budget key must be 1–64 lowercase account identifier characters")


class Store:
    def __init__(self, path: str | Path) -> None:
        self._init_state()
        self.path = Path(path)
        if str(path) == ":memory:" or self.path.is_symlink():
            raise ValueError("Store requires an operator-owned regular local database path")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = self._connect()
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] not in (0, SCHEMA_VERSION):
                raise DomainError("release_mismatch", "Database schema requires explicit isolated conversion")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise DomainError("release_mismatch", "Database schema requires explicit isolated conversion")
            if version == 0:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                    raise DomainError("release_mismatch", "Unversioned nonempty database cannot be adopted")
                for statement in SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        os.chmod(self.path, 0o600)

    def _init_state(self) -> None:
        self.record_payloads: RecordPayloads | None = None
        self._shared_bodies = _SharedBodies()
        self._read_bodies: ContextVar[tuple[_ReadBodies, ...]] = ContextVar('store_read_bodies', default=())

    @contextmanager
    def _transaction_scope(self, conn: sqlite3.Connection, *, write: bool) -> Iterator[None]:
        """Share read bodies for the scope of a read transaction."""
        bodies = None if write else _ReadBodies(conn)
        read_token = self._read_bodies.set((*self._read_bodies.get(), bodies)) if bodies is not None else None
        try:
            yield
        finally:
            if read_token is not None:
                self._read_bodies.reset(read_token)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """One connection per scope/thread; rollback all work if the body raises."""
        conn = self._connect()
        try:
            if not write:
                conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with self._transaction_scope(conn, write=write):
                yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _using(self, conn: sqlite3.Connection | None, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        if conn is not None:
            if not conn.in_transaction:
                raise ValueError("Caller connection must have an active transaction")
            savepoint = new_id("scope")
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
        else:
            with self.transaction(write=write) as own:
                yield own

    def _project(self, conn: sqlite3.Connection, project_id: str) -> None:
        if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
            raise DomainError("not_found", "Project not found")

    def create_project(self, project_id: str, body: Any, author: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self._using(conn) as db:
            if db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise DomainError("revision_conflict", "Project already exists", current_revision=1)
            db.execute("INSERT INTO projects(project_id) VALUES (?)", (project_id,))
            return self.create_object(project_id, "project", body, author, object_id=project_id, conn=db)

    def encode_record(self, kind: str, body: Any) -> str:
        physical = self.record_payloads.encode(kind, body) if self.record_payloads else body
        return canonical_json(physical)

    def decode_record(self, physical: Any, digest: str) -> Any:
        return self.record_payloads.decode(physical, digest) if self.record_payloads else physical

    def encode_replay(self, result: Any) -> str:
        if self.record_payloads is None:
            return canonical_json(result)
        logical = {'result': result}
        physical = self.record_payloads.encode('idempotency-result', logical)
        # Always wrap opted-in replay values: even a literal marker-shaped
        # result is returned unchanged. Legacy unwrapped results stay readable.
        return canonical_json({'$mvgp_replay_v1': {'digest': content_hash(logical), 'body': physical}})

    def decode_replay(self, encoded: str) -> Any:
        value = json.loads(encoded)
        if not isinstance(value, dict) or set(value) != {'$mvgp_replay_v1'}:
            return value
        if self.record_payloads is None:
            raise DomainError('missing_prerequisite', 'External replay storage is not configured')
        record = value['$mvgp_replay_v1']
        if not isinstance(record, dict) or set(record) != {'digest', 'body'}:
            raise DomainError('provider_failure', 'Stored replay envelope is invalid')
        logical = self.decode_record(record['body'], record['digest'])
        if not isinstance(logical, dict) or set(logical) != {'result'}:
            raise DomainError('provider_failure', 'Stored replay result is invalid')
        return logical['result']

    def create_object(self, project_id: str, kind: str, body: Any, author: str, *, object_id: str | None = None,
                      conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        object_id = object_id or new_id("obj")
        encoded, digest = self.encode_record(kind, body), content_hash(body)
        with self._using(conn) as db:
            self._project(db, project_id)
            if db.execute("SELECT 1 FROM objects WHERE object_id=?", (object_id,)).fetchone():
                raise DomainError("revision_conflict", "Object already exists")
            db.execute("INSERT INTO objects VALUES (?,?,?,1)", (project_id, object_id, kind))
            db.execute("INSERT INTO revisions(project_id,object_id,revision,body,digest,author) VALUES (?,?,1,?,?,?)", (project_id, object_id, encoded, digest, author))
            self.append_event(project_id, "object.created", {"object_id": object_id, "revision": 1}, conn=db)
            return self.get_object(project_id, object_id, conn=db)

    def append_revision(self, project_id: str, object_id: str, expected_revision: int, body: Any, author: str,
                        *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 1:
            raise DomainError("invalid_input", "Expected revision must be a positive integer")
        digest = content_hash(body)
        with self._using(conn) as db:
            current = self.get_object(project_id, object_id, conn=db)
            encoded = self.encode_record(current["kind"], body)
            changed = db.execute("UPDATE objects SET current_revision=current_revision+1 WHERE project_id=? AND object_id=? AND current_revision=?", (project_id, object_id, expected_revision)).rowcount
            if changed != 1:
                raise DomainError("revision_conflict", "Object changed", current_revision=current["revision"], repair="Read current revision")
            revision = expected_revision + 1
            db.execute("INSERT INTO revisions(project_id,object_id,revision,body,digest,author) VALUES (?,?,?,?,?,?)", (project_id, object_id, revision, encoded, digest, author))
            self.append_event(project_id, "object.revised", {"object_id": object_id, "revision": revision}, conn=db)
            return self.get_object(project_id, object_id, conn=db)

    def get_object(self, project_id: str, object_id: str, *, revision: int | None = None,
                   conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self._using(conn, write=False) as db:
            row = db.execute("""SELECT r.*,o.kind FROM objects o JOIN revisions r
                ON o.project_id=r.project_id AND o.object_id=r.object_id
                WHERE o.project_id=? AND o.object_id=? AND r.revision=COALESCE(?,o.current_revision)""", (project_id, object_id, revision)).fetchone()
            if row is None:
                raise DomainError("not_found", "Object revision not found")
            result = dict(row)
            # Always fetch the SQL row from this snapshot. Only decoding is reused;
            # no cached current revision, permission or graph verdict is trusted.
            cache = next((entry for entry in reversed(self._read_bodies.get()) if entry.connection is db), None)
            def expand(physical: Any, digest: str) -> Any:
                return self._shared_bodies.decode((project_id, object_id, result['revision'], digest),
                                                  physical, self.decode_record)
            result["body"] = (expand(json.loads(result["body"]), result['digest']) if cache is None else
                              cache.decode((project_id, object_id, result['revision'], result['digest']),
                                           result['body'], expand))
            return result

    def list_objects(self, project_id: str, *, kind: str | None = None,
                     conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._using(conn, write=False) as db:
            self._project(db, project_id)
            rows = db.execute("SELECT object_id FROM objects WHERE project_id=? AND (? IS NULL OR kind=?) ORDER BY object_id", (project_id, kind, kind)).fetchall()
            return [self.get_object(project_id, row[0], conn=db) for row in rows]

    def history(self, project_id: str, object_id: str, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._using(conn, write=False) as db:
            self.get_object(project_id, object_id, conn=db)
            rows = db.execute("SELECT revision FROM revisions WHERE project_id=? AND object_id=? ORDER BY revision", (project_id, object_id)).fetchall()
            return [self.get_object(project_id, object_id, revision=row[0], conn=db) for row in rows]

    def append_event(self, project_id: str, kind: str, body: Any, *, conn: sqlite3.Connection | None = None) -> int:
        with self._using(conn) as db:
            self._project(db, project_id)
            cursor = db.execute("INSERT INTO events(project_id,kind,body) VALUES (?,?,?)", (project_id, kind, canonical_json(body)))
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def events(self, project_id: str, *, after: int = 0, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._using(conn, write=False) as db:
            self._project(db, project_id)
            rows = db.execute("SELECT * FROM events WHERE project_id=? AND sequence>? ORDER BY sequence", (project_id, after)).fetchall()
            return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def run_idempotent(self, scope: str, key: str, payload: Any, action: Callable[[sqlite3.Connection], Any],
                       *, conn: sqlite3.Connection | None = None) -> Any:
        """Scope must include authenticated principal/project/operation, built server-side."""
        digest = content_hash(payload)
        with self._using(conn) as db:
            row = db.execute("SELECT request_hash,result FROM idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
            if row:
                if row["request_hash"] != digest:
                    raise DomainError("idempotency_conflict", "Key already used for different content")
                return self.decode_replay(row["result"])
            result = action(db)
            encoded = self.encode_replay(result)
            db.execute("INSERT INTO idempotency VALUES (?,?,?,?)", (scope, key, digest, encoded))
            return self.decode_replay(encoded)

    def set_budget(self, project_id: str, limit: int, unit: str, *, budget_key: str = "legacy",
                   conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Trusted envelope change; native units are immutable within each account.

        The default is historical/fake compatibility, never automatic live routing.
        This function grants no caller authority to select an execution account.
        """
        _amount(limit)
        _budget_key(budget_key)
        if not isinstance(unit, str) or not unit.strip():
            raise DomainError("invalid_input", "Budget unit is required")
        with self._using(conn) as db:
            self._project(db, project_id)
            current = db.execute("SELECT * FROM budgets WHERE project_id=? AND budget_key=?", (project_id, budget_key)).fetchone()
            if current and (unit != current["unit"] or limit < current["spent"] + current["reserved"]):
                raise DomainError("budget_exceeded", "Cannot change unit or lower envelope below commitments")
            db.execute("""INSERT INTO budgets(project_id,budget_key,ceiling,unit) VALUES (?,?,?,?)
                ON CONFLICT(project_id,budget_key) DO UPDATE SET ceiling=excluded.ceiling""", (project_id, budget_key, limit, unit))
            if current is None or current["ceiling"] != limit:
                self.append_event(project_id, "budget.changed", {"budget_key": budget_key, "ceiling": limit, "unit": unit}, conn=db)
            return self.budget(project_id, budget_key=budget_key, conn=db)

    def budget(self, project_id: str, *, budget_key: str = "legacy",
               conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        _budget_key(budget_key)
        with self._using(conn, write=False) as db:
            row = db.execute("SELECT * FROM budgets WHERE project_id=? AND budget_key=?", (project_id, budget_key)).fetchone()
            if row is None:
                raise DomainError("missing_prerequisite", "Project has no spending envelope for this account")
            return dict(row)

    def list_budgets(self, project_id: str, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        """Return separately denominated envelopes; never total unlike native units."""
        with self._using(conn, write=False) as db:
            self._project(db, project_id)
            return [dict(row) for row in db.execute("SELECT * FROM budgets WHERE project_id=? ORDER BY budget_key", (project_id,))]

    def reserve(self, project_id: str, reservation_id: str, amount: int, unit: str, *, object_id: str | None = None,
                budget_key: str = "legacy", conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Bind a project-unique attempt to exactly one native account atomically."""
        _amount(amount)
        _budget_key(budget_key)
        with self._using(conn) as db:
            previous = db.execute("SELECT * FROM reservations WHERE project_id=? AND reservation_id=?", (project_id, reservation_id)).fetchone()
            if previous and (previous["budget_key"] != budget_key or previous["amount"] != amount or previous["object_id"] != object_id):
                raise DomainError("idempotency_conflict", "Reservation already has different intent or account")
            budget = self.budget(project_id, budget_key=budget_key, conn=db)
            if budget["unit"] != unit:
                raise DomainError("invalid_input", "Reservation unit differs from envelope")
            if previous:
                return dict(previous)
            if amount > budget["ceiling"] - budget["reserved"] - budget["spent"]:
                raise DomainError("budget_exceeded", "Insufficient unreserved budget")
            db.execute("UPDATE budgets SET reserved=reserved+? WHERE project_id=? AND budget_key=?", (amount, project_id, budget_key))
            db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,NULL,'held')", (project_id, reservation_id, budget_key, object_id, amount))
            self.append_event(project_id, "budget.reserved",
                              {"budget_key": budget_key, "reservation_id": reservation_id, "amount": amount, "unit": unit}, conn=db)
            return dict(db.execute("SELECT * FROM reservations WHERE project_id=? AND reservation_id=?", (project_id, reservation_id)).fetchone())

    def settle(self, project_id: str, reservation_id: str, actual_amount: int | None,
               *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Derive the account from the reservation, never a caller-supplied account.

        None retains the full hold as unknown; zero needs confirmed no cost.
        A genuine billed overrun is recorded, never silently capped to the estimate.
        Future reservations then fail until a trusted envelope decision covers it.
        """
        if actual_amount is not None:
            _amount(actual_amount)
        with self._using(conn) as db:
            row = db.execute("SELECT * FROM reservations WHERE project_id=? AND reservation_id=?", (project_id, reservation_id)).fetchone()
            if row is None:
                raise DomainError("not_found", "Reservation not found")
            budget_key = row["budget_key"]
            current = self.budget(project_id, budget_key=budget_key, conn=db)
            if row["state"] == "settled":
                if row["actual"] != actual_amount:
                    raise DomainError("idempotency_conflict", "Reservation already settled differently")
                return current
            if actual_amount is None:
                db.execute("UPDATE reservations SET state='unknown' WHERE project_id=? AND reservation_id=?", (project_id, reservation_id))
            else:
                _amount(current["spent"] + actual_amount)
                db.execute("UPDATE budgets SET reserved=reserved-?,spent=spent+? WHERE project_id=? AND budget_key=?",
                           (row["amount"], actual_amount, project_id, budget_key))
                db.execute("UPDATE reservations SET state='settled',actual=? WHERE project_id=? AND reservation_id=?", (actual_amount, project_id, reservation_id))
            if actual_amount is not None or row["state"] != "unknown":
                self.append_event(project_id, "budget.settled" if actual_amount is not None else "budget.unknown",
                                  {"budget_key": budget_key, "unit": current["unit"], "reservation_id": reservation_id, "actual": actual_amount}, conn=db)
            return self.budget(project_id, budget_key=budget_key, conn=db)

    def backup(self, destination: str | Path) -> None:
        """Consistent SQLite snapshot; metadata only, never a full object-store backup."""
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
        os.close(descriptor)
        source, output = self._connect(), sqlite3.connect(temporary)
        try:
            source.backup(output)
            output.close()
            with open(temporary, "rb") as snapshot:
                os.fsync(snapshot.fileno())
            os.link(temporary, target)  # Atomic publication without replacing a racing target.
        finally:
            source.close()
            output.close()
            os.unlink(temporary)
