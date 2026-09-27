"""Check a release's config dir with the new code before any service stops.

The config models alone passed release 3's first attempt, yet its worker refused to start: a worker's lease must
exceed twice the longest provider call timeout plus 5 s (production/worker.py Worker.__init__), and the prepared
config raised the apilio timeout without the lease. This runs the models and that rule on every config dir, also one
prepared ahead of the release, so such a config stops the release while the old one still runs.
  PYTHONPATH=<code dir> <python> studio/check_config.py <api.json> <worker.json>   exit 0 ok, 1 with the problems
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def problems(api: dict[str, Any], worker: dict[str, Any]) -> list[str]:
    from pydantic import ValidationError

    from production.server import ServerConfiguration
    from production.worker import WorkerConfiguration
    found: list[str] = []
    try:
        ServerConfiguration.model_validate(api)
    except ValidationError as exc:
        found.append(f'api.json does not match the code: {exc.error_count()} error(s), first at '
                     f"{'.'.join(map(str, exc.errors()[0]['loc']))}")
    try:
        config = WorkerConfiguration.model_validate(worker)
    except ValidationError as exc:
        found.append(f'worker.json does not match the code: {exc.error_count()} error(s), first at '
                     f"{'.'.join(map(str, exc.errors()[0]['loc']))}")
        return found
    timeouts = {name: section.timeout for name, section in (('apilio_images', config.apilio_images), ('fal', config.fal),
                                                            ('hf', config.hf))
                if section is not None}
    if timeouts and config.lease_seconds <= 2 * max(timeouts.values()) + 5:
        name = max(timeouts, key=timeouts.__getitem__)
        found.append(f'worker lease_seconds {config.lease_seconds} must exceed 2 × {name}.timeout {timeouts[name]} + 5 '
                     f'= {2 * timeouts[name] + 5} (the worker refuses to start otherwise)')
    return found


def main(argv: list[str]) -> int:
    api, worker = (json.loads(Path(p).read_text()) for p in argv[:2])
    found = problems(api, worker)
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
