"""Rehearsal stand-in for the Higgsfield CLI.
Spends nothing, calls nothing. Installed with version.txt and capabilities/ beside it by
`tools/rehearsal/rehearse.py install_fake_hf`. Every create is appended whole to HOME/creates.jsonl, so the loop can
check that the prompt Higgsfield would get is the candidate's (the writer text plus the allowed additions).

Answers only what the platform's provider asks: --version, model get, generate cost|create|get|list (all with --json).
A created video job completes at once. Its result URL names a 16:9 1080p clip of the requested length on the HF CDN
host; the rehearsal worker's downloader (tools/rehearsal/fakes.py MediaServer) makes that clip locally, so the
platform's real download, probe and conformance path runs without the network.
"""
import datetime
import fcntl
import json
import os
import pathlib
import sys
import uuid

D = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path(os.environ['HOME'])
STATE = HOME / 'jobs.json'
HF_CDN = 'd8j0ntlcm91z4.cloudfront.net'  # Higgsfield's result host, on the worker's download allowlist


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def out(value):
    sys.stdout.write(json.dumps(value))
    sys.exit(0)


args = sys.argv[1:]
with (HOME / 'calls.log').open('a') as log:
    log.write(json.dumps({'at': datetime.datetime.now().isoformat(), 'args': args[:3]}) + '\n')
if args == ['--version']:
    print((D / 'version.txt').read_text().strip())
    sys.exit(0)
if not args or args[-1] != '--json':
    sys.exit(2)
args = args[:-1]
if args[:2] == ['model', 'get']:
    out(json.loads((D / 'capabilities' / f'{args[2]}.json').read_text()))
if args[:2] == ['generate', 'cost']:
    out({'job_type': args[2], 'credits': 0, 'dry_run': True})
if args[:2] == ['generate', 'create']:
    flags, rest, media = {}, args[3:], {}
    for name, value in zip(rest[0::2], rest[1::2]):
        flags.setdefault(name, value)
        if name.endswith('-references'):
            media[name] = media.get(name, 0) + 1
    with (HOME / 'creates.jsonl').open('a') as log:
        log.write(json.dumps({'job_type': args[2], 'flags': flags, 'references': media}) + '\n')
    # The worker sends creates in parallel; one lock keeps every job (2026-09-27 rehearsal: an unlocked
    # read-modify-write lost one of twelve, which the platform then correctly kept polling as unknown).
    with (HOME / 'jobs.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        jobs = load()
        job_id = str(uuid.uuid4())
        jobs[job_id] = {'job_type': args[2], 'duration': int(flags.get('--duration', 5)), 'prompt': flags.get('--prompt', ''),
                        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'), 'n': len(jobs)}
        STATE.write_text(json.dumps(jobs))
    out([job_id])
if args[:2] == ['generate', 'get']:
    job = load().get(args[2])
    if job is None:
        sys.exit(1)
    url = f"https://{HF_CDN}/rehearsal/video-1920x1080-{job['duration']}s-a.mp4"
    out({'id': args[2], 'job_type': job['job_type'], 'status': 'completed', 'params': {},
         'result_url': url, 'created_at': job['created_at']})
if args[:2] == ['generate', 'list']:
    out([{'id': k, 'job_type': v['job_type'], 'created_at': v['created_at'],
          'params': {'prompt': v['prompt'], 'duration': v['duration']}} for k, v in load().items()])
sys.exit(3)
