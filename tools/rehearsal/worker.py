"""The rehearsal worker: the real worker with the paid providers and result hosts swapped for fakes.

Run by `tools/rehearsal/rehearse.py up` with PYTHONPATH=<code dir>:<repo>, so `production` is the code under test.
Everything the platform does runs for real (claims, dispatch intents, reservations, receipts, cost settlement,
downloads, probes, conformance, completions, the film); only the three outward edges change:
  - fal and apilio adapters send to `tools/rehearsal/fakes.py` instead of the network;
  - the result downloader serves files the fakes made, never the network;
  - the source reader's Gemini call answers from `fake_reader`;
  - AutoComplete waits MVGP_COMPLETE_DELAY seconds after a pick instead of ten minutes.
It refuses a database outside the rehearsal copy.
"""
import argparse
import json
import os
import sys
import threading
from pathlib import Path

import functools

from production import reader as reader_module
from production.jobs import SafeDownloader
from production.provider_apilio import ApilioImages
from production.provider_fal import FalSeedance
from production.worker import build_worker
from tools.rehearsal.fakes import FakeApilio, FakeFal, MediaServer, fake_reader


def install(worker, media: MediaServer, hosts: list[str]) -> dict:
    fal, apilio = FakeFal(media, log=os.environ.get('MVGP_REHEARSAL_FAL_LOG') or None), FakeApilio(media)
    swapped = {}
    provider = worker.provider
    for job_type, adapter in (provider.adapters if provider is not None else {}).items():
        if isinstance(adapter, FalSeedance):
            adapter.transport = fal
        elif isinstance(adapter, ApilioImages):
            adapter.transport = apilio
        else:
            continue
        swapped[job_type] = type(adapter).__name__
    # The source reader's Gemini call (live mode keeps its own checks; only the HTTP edge is replaced).
    reader_module._http = functools.partial(fake_reader, model=reader_module.MODEL)
    swapped['reader'] = 'fake_reader'
    # Public-address check stays; nothing resolves or connects.
    worker.jobs.downloader = SafeDownloader(set(hosts), resolver=lambda host: ['93.184.216.34'], transport=media.serve)
    if worker.complete is not None:
        worker.complete.DELAY_SECONDS = float(os.environ.get('MVGP_COMPLETE_DELAY', '5'))
    return swapped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if '/rehearsal/' not in config['storage']['database']:
        raise SystemExit('refusing: the rehearsal worker runs only on the rehearsal copy')
    worker = build_worker(args.config)
    swapped = install(worker, MediaServer(Path(os.environ['MVGP_REHEARSAL_MEDIA'])), config['download_hosts'])
    print(json.dumps({'rehearsal': True, 'fakes': swapped, **worker.configuration_status}), flush=True)
    stop = threading.Event()
    try:
        worker.run(stop, poll_interval=getattr(worker, 'poll_interval', 1.0))
    except KeyboardInterrupt:
        stop.set()
    finally:
        worker.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
