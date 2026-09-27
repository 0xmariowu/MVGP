"""Record the reshoot cutoff in a worker config.

Run with both services stopped, just before the new worker starts: the highest event number of the database goes
into the worker config as `reshoot_after_event`. Only a 再拍一批 the owner answers after it re-fires by itself, so
nothing answered before a deploy is ever shot again. Prints the cutoff.
  <studio>/venv/bin/python studio/reshoot_cutoff.py --db <metadata.sqlite> --worker-config <worker.json>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from studio.idle_check import connect_ro
except ImportError:  # run as a script from studio/
    from idle_check import connect_ro  # type: ignore[no-redef]


def record(database: str | Path, worker_config: str | Path) -> int:
    db = connect_ro(str(database))
    try:
        cutoff = int(db.execute('SELECT COALESCE(MAX(sequence), 0) FROM events').fetchone()[0])
    finally:
        db.close()
    path = Path(worker_config)
    value = {**json.loads(path.read_text()), 'reshoot_after_event': cutoff}
    fd = os.open(f'{path}.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=1)
    os.replace(f'{path}.tmp', path)
    return cutoff


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--worker-config', required=True)
    args = parser.parse_args()
    print(record(args.db, args.worker_config))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
