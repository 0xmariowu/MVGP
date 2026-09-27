"""Explicit service storage bootstrap: one local SQLite database and one media folder.

Local mode opens only an existing private database; development mode (tests, rehearsal copies) may create one.
There is no release activation binding: a deploy stops the services, swaps the code dir and
config and starts them again.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from production.contracts import Contract, DomainError
from production.media import MediaStore
from production.record_payloads import local_record_payloads
from production.store import SCHEMA_VERSION, Store

PUBLIC_UPLOAD_BYTES = 90 * 1024 * 1024
INTERNAL_MEDIA_BYTES = 512 * 1024 * 1024
ServiceRole = Literal['api', 'worker', 'operator']


class StorageConfiguration(Contract):
    mode: Literal['development', 'local']
    media_root: str
    database: str


def private_path(value: str | Path, *, directory: bool = False, create: bool = False) -> Path:
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts
            or any(item.is_symlink() for item in (path, *path.parents))):
        raise DomainError('forbidden', 'Storage paths must be absolute and contain no symlinks')
    if directory and create:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.exists():
        info = path.stat()
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid_type or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DomainError('forbidden', 'Storage must be private and owned by the service identity')
    else:
        raise DomainError('missing_prerequisite', 'Explicit storage path does not exist')
    return path


def _existing_schema(database: Path) -> None:
    """Read before Store can initialize a schema: a local service never turns an empty or foreign file into a store."""
    db = sqlite3.connect('file:'+quote(str(database), safe='/')+'?mode=ro', uri=True)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            raise ValueError
    except (sqlite3.DatabaseError, ValueError):
        raise DomainError('release_mismatch', 'Existing production database lacks the exact schema') from None
    finally:
        db.close()


@dataclass
class RuntimeStorage:
    store: Store
    media: MediaStore
    mode: str
    role: ServiceRole
    journal: None = None  # the cloud response journal is gone; kept None for legacy readers

    def readiness(self) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise DomainError('release_mismatch', 'Storage schema is incompatible')
        return {'mode': self.mode, 'schema_version': SCHEMA_VERSION}

    def close(self) -> None:
        return None


def open_storage(config: StorageConfiguration, *, role: ServiceRole) -> RuntimeStorage:
    """The caller supplies the service role; no maker request controls this input."""
    if role not in ('api', 'worker', 'operator'):
        raise DomainError('forbidden', 'Unknown service storage role')
    operator = role == 'operator'
    media_root = private_path(config.media_root, directory=True, create=True)
    maximum = PUBLIC_UPLOAD_BYTES if role == 'api' else INTERNAL_MEDIA_BYTES
    database = Path(config.database)
    private_path(database.parent, directory=True)
    # Only explicit operator/development bootstrap may create a DB.
    if database.exists() or not (operator or config.mode == 'development'):
        private_path(database)
    elif not database.is_absolute() or '..' in database.parts or database.is_symlink():
        raise DomainError('forbidden', 'Invalid local database path')
    if not operator and config.mode == 'local':
        _existing_schema(database)
    store = Store(database)
    if config.mode == 'local':
        store.record_payloads = local_record_payloads(database)
    media = MediaStore(store, media_root, max_bytes=maximum)
    result = RuntimeStorage(store, media, config.mode, role)
    result.readiness()
    return result
