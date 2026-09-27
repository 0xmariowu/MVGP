"""Pull the providers' own usage records into daily snapshots (the 账本).

fal: `GET https://api.fal.ai/v1/models/usage` (fal docs, platform-apis/v1/models/usage, read 2026-09-27): daily buckets
of `results` per endpoint with `quantity`, `unit_price`, `cost_total` and `currency`; paginated by `next_cursor`. It
needs an ADMIN key (`FAL_AI_ADMIN_TOKEN`): the worker's `FAL_AI_TOKEN` answers 403 "This API key is not permitted to
perform this action" (checked 2026-09-27). apilio: `GET https://api.apilio.ai/v1/dashboard/billing/usage` answers
`{"object", "total_usage"}`, a running total in 人民币分 (memory: reference_apilio-billing-and-ceiling; checked
2026-09-27), so one snapshot a day and the difference between two is that day's spend.

Higgsfield (owner 2026-09-27 "你应该统一记账啊"): the pinned CLI, run as the worker runs it
(the worker config's `hf` native_path and credential_home as HOME), `account transactions --size 100 --json` paged by
`cursor` — each item {action, created_at, credits, display_name}, no job id (checked 2026-09-27) — and `account status`
for today's balance. The CLI's sign-in stays in its HOME; nothing of it is read or written here.

Snapshots go to `<studio>/state/live/ledger/<provider>-<YYYY-MM-DD>.json` (0600, folder 0700): files, no new store
kind. A missing key or a refused call is written as the snapshot's `error`, never hidden. Keys are read at call time
from the environment or `--env-file` (a JSON object like `env/live-worker.json`), sent only in the Authorization
header to the two fixed hosts, and never logged or written. Read-only towards both providers; nothing is paid.
  <studio>/venv/bin/python studio/usage_pull.py --studio <studio> [--env-file <json>...] [--days 7]
The live keys: APILIO_AI_KEY is in env/live-worker.json; the fal admin key goes in env/live-ledger.json (owner's call).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

FAL_USAGE = 'https://api.fal.ai/v1/models/usage'
APILIO_USAGE = 'https://api.apilio.ai/v1/dashboard/billing/usage'
FAL_KEY, APILIO_KEY = 'FAL_AI_ADMIN_TOKEN', 'APILIO_AI_KEY'
MAX_PAGES, MAX_BYTES = 20, 4 * 1024 * 1024
# (url, headers) -> (status, body bytes); injected in tests.
Transport = Callable[[str, dict[str, str]], tuple[int, bytes]]
# Higgsfield CLI arguments (without --json) -> (exit code, stdout bytes); injected in tests.
Runner = Callable[[list[str]], tuple[int, bytes]]
HF_SOURCE = 'higgsfield account transactions'
HF_PAGE = 100


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect and carry the Authorization header to the new host; never follow one."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={**headers, 'User-Agent': 'Mozilla/5.0 (mvgp usage pull)'})
    try:
        with _OPENER.open(request, timeout=60) as response:  # two fixed https hosts
            return response.status, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_BYTES + 1)


def _json(status: int, body: bytes) -> tuple[Any, str | None]:
    if len(body) > MAX_BYTES:
        return None, 'answer exceeded its bound'
    try:
        data = json.loads(body)
    except ValueError:
        return None, f'unreadable answer (HTTP {status})'
    if status != 200:
        message = (data.get('error') or {}).get('message') if isinstance(data, dict) and isinstance(data.get('error'), dict) else None
        return None, f'HTTP {status}' + (f': {str(message)[:200]}' if message else '')
    return data, None


def fal_days(key: str | None, start: dt.date, end: dt.date, transport: Transport) -> dict[str, dict[str, Any]]:
    """Per UTC day: fal's own results (endpoint, unit, quantity, unit_price, cost_total, currency), or an error."""
    days = [(start + dt.timedelta(n)).isoformat() for n in range((end - start).days)]
    if not key:
        return {day: {'error': f'{FAL_KEY} is not set; fal usage needs an admin key'} for day in days}
    found: dict[str, list[dict[str, Any]]] = {day: [] for day in days}
    params = {'start': start.isoformat(), 'end': end.isoformat(), 'timeframe': 'day', 'timezone': 'UTC', 'expand': 'time_series'}
    for _ in range(MAX_PAGES):
        data, error = _json(*transport(f'{FAL_USAGE}?{urllib.parse.urlencode(params)}', {'Authorization': f'Key {key}'}))
        if error:
            return {day: {'error': error} for day in days}
        for bucket in data.get('time_series') or []:
            day = str(bucket.get('bucket', ''))[:10]
            if day in found:
                found[day].extend({k: r.get(k) for k in ('endpoint_id', 'unit', 'quantity', 'unit_price', 'cost_total', 'currency')}
                                  for r in bucket.get('results') or [] if isinstance(r, dict))
        if not data.get('has_more') or not data.get('next_cursor'):
            return {day: {'results': rows} for day, rows in found.items()}
        params['cursor'] = str(data['next_cursor'])
    return {day: {'error': f'more than {MAX_PAGES} pages'} for day in days}


def apilio_total(key: str | None, transport: Transport) -> dict[str, Any]:
    """apilio's running total in 人民币分 at this moment, or an error."""
    if not key:
        return {'error': f'{APILIO_KEY} is not set'}
    data, error = _json(*transport(APILIO_USAGE, {'Authorization': f'Bearer {key}'}))
    if error:
        return {'error': error}
    total = data.get('total_usage') if isinstance(data, dict) else None
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return {'error': 'no total_usage in the answer'}
    return {'total_usage': total, 'unit': '人民币分'}


def hf_runner(native: Path, home: Path) -> Runner:
    """The pinned CLI with only its own HOME, as the worker's adapter runs it; bounded time and output."""
    def run(args: list[str]) -> tuple[int, bytes]:
        try:
            done = subprocess.run([str(native), *args, '--json'], env={'HOME': str(home), 'PATH': '/usr/bin:/bin', 'NO_COLOR': '1'},
                                  capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(type(exc).__name__).encode()
        return done.returncode, done.stdout[:MAX_BYTES + 1]
    return run


def higgsfield_days(run: Runner | None, start: dt.date, end: dt.date) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Per UTC day: Higgsfield's own transactions (action, created_at, credits, display_name), or an error; and the
    balance now."""
    days = [(start + dt.timedelta(n)).isoformat() for n in range((end - start).days)]
    if run is None:
        missing = {'error': 'no Higgsfield CLI configured (the worker config has no hf section)'}
        return {day: dict(missing) for day in days}, dict(missing)
    def call(args: list[str]) -> tuple[Any, str | None]:
        code, body = run(args)
        if code != 0:
            return None, f'higgsfield {" ".join(args[:2])} exited {code}'
        return _json(200, body)
    found: dict[str, list[dict[str, Any]]] = {day: [] for day in days}
    args = ['account', 'transactions', '--size', str(HF_PAGE)]
    for _ in range(MAX_PAGES):
        data, error = call(args)
        if error:
            return {day: {'error': error} for day in days}, {'error': error}
        items = [i for i in (data.get('items') or []) if isinstance(i, dict)] if isinstance(data, dict) else []
        for item in items:
            day = str(item.get('created_at', ''))[:10]
            if day in found:
                found[day].append({k: item.get(k) for k in ('action', 'created_at', 'credits', 'display_name')})
        oldest = min((str(i.get('created_at', '')) for i in items), default='')
        cursor = data.get('cursor') if isinstance(data, dict) else None
        if not items or not cursor or oldest[:10] < start.isoformat():
            break
        args = ['account', 'transactions', '--size', str(HF_PAGE), '--cursor', str(cursor)]
    else:
        return {day: {'error': f'more than {MAX_PAGES} pages'} for day in days}, {'error': f'more than {MAX_PAGES} pages'}
    status, error = call(['account', 'status'])
    credits = status.get('credits') if isinstance(status, dict) else None
    balance = ({'error': error} if error else {'credits': credits} if isinstance(credits, (int, float))
               and not isinstance(credits, bool) else {'error': 'no credits in account status'})
    return {day: {'transactions': rows} for day, rows in found.items()}, balance


def _write(folder: Path, name: str, value: dict[str, Any]) -> Path:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / name
    fd = os.open(f'{path}.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=1, ensure_ascii=False)
    os.replace(f'{path}.tmp', path)
    return path


def pull(studio: Path, keys: Mapping[str, str], *, days: int = 7, transport: Transport = _http,
         now: dt.datetime | None = None, hf_run: Runner | None = None) -> list[Path]:
    """Write fal's last `days` finished UTC days plus today so far, and apilio's running total for today."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today, folder = now.date(), Path(studio) / 'state/live/ledger'
    taken = now.isoformat().replace('+00:00', 'Z')
    written = []
    for day, value in fal_days(keys.get(FAL_KEY), today - dt.timedelta(days), today + dt.timedelta(1), transport).items():
        written.append(_write(folder, f'fal-{day}.json', {'provider': 'fal', 'day': day, 'taken_at': taken,
                                                          'source': FAL_USAGE, 'complete': day < today.isoformat(), **value}))
    written.append(_write(folder, f'apilio-{today.isoformat()}.json', {'provider': 'apilio', 'day': today.isoformat(),
                                                                       'taken_at': taken, 'source': APILIO_USAGE,
                                                                       **apilio_total(keys.get(APILIO_KEY), transport)}))
    hf_days, balance = higgsfield_days(hf_run, today - dt.timedelta(days), today + dt.timedelta(1))
    for day, value in hf_days.items():
        extra = {'balance': balance} if day == today.isoformat() else {}
        written.append(_write(folder, f'higgsfield-{day}.json', {'provider': 'higgsfield', 'day': day, 'taken_at': taken,
                                                                  'source': HF_SOURCE, 'complete': day < today.isoformat(),
                                                                  **value, **extra}))
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--studio', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, action='append', default=[], help='JSON object of keys; may repeat')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--worker-config', type=Path, help="the live worker config; its `hf` section names the CLI and HOME")
    args = parser.parse_args()
    keys = {name: os.environ[name] for name in (FAL_KEY, APILIO_KEY) if os.environ.get(name)}
    for env_file in args.env_file:
        if env_file.exists():
            stored = json.loads(env_file.read_text())
            keys.update({name: stored[name] for name in (FAL_KEY, APILIO_KEY) if isinstance(stored.get(name), str) and stored[name]})
    hf_run = None
    if args.worker_config and args.worker_config.exists():
        hf = json.loads(args.worker_config.read_text()).get('hf')
        if isinstance(hf, dict) and hf.get('native_path') and hf.get('credential_home'):
            hf_run = hf_runner(Path(hf['native_path']), Path(hf['credential_home']))
    for path in pull(args.studio, keys, days=max(1, min(args.days, 31)), hf_run=hf_run):
        snapshot = json.loads(path.read_text())
        print(path.name, 'error: ' + snapshot['error'] if 'error' in snapshot else 'ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
