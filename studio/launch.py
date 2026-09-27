"""Start one live MVGP service on this Mac: api | worker | tunnel.

What runs is named in `<studio>/current.json`:
{"code_dir": ".../runtime/lean-<sha>", "api_config": ".../api.json", "worker_config": ".../worker.json"}.
A code dir named `release-*` is an old release tree: the archived old launcher runs it with the untouched env
files, so a rollback to release 97 needs nothing else. A lean code dir gets only the keys lean code reads, taken
from the private env file (`env/<target>-<role>.json`), which is never edited. Secrets never reach argv or logs.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

STUDIO = Path(os.environ.get('MVGP_STUDIO') or '/Users/Shared/mvgp-studio').expanduser()
PY = STUDIO / 'venv' / 'bin' / 'python'
CLOUDFLARED = os.environ.get('MVGP_CLOUDFLARED') or shutil.which('cloudflared') or '/opt/homebrew/bin/cloudflared'
FFMPEG_BIN = os.environ.get('MVGP_FFMPEG_BIN') or str(
    Path(shutil.which('ffmpeg') or '/opt/homebrew/Cellar/ffmpeg/9.0.2/bin/ffmpeg').parent)
OLD_LAUNCHER = STUDIO / 'launch-release.py'
# The only secrets lean code reads (no Access read token, vault keyring or legacy reader credentials).
LEAN_KEYS = {'api': ('MVGP_PORT',),
             'worker': ('APILIO_AI_KEY', 'FAL_AI_TOKEN', 'MVGP_FILM_TOKEN', 'MVGP_WORKER_TOKEN')}


def private_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SystemExit(f'{path} must be a private regular file owned by this user')
        return f.read()


def current(target: str) -> dict[str, str]:
    path = STUDIO / 'current.json'
    value = json.loads(private_file(path))
    for key in ('code_dir', 'api_config', 'worker_config'):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise SystemExit(f'{path}: {key} must be an absolute path')
    return value


def lean_env(role: str, target: str, secrets: dict[str, str], code_dir: str, base: dict[str, str]) -> dict[str, str]:
    missing = [k for k in LEAN_KEYS[role] if k not in secrets]
    if missing:
        raise SystemExit(f'env/{target}-{role}.json lacks {", ".join(missing)}')
    return {**base, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': code_dir, 'PYTHONUNBUFFERED': '1',
            **{k: secrets[k] for k in LEAN_KEYS[role] if k in secrets}}


def main() -> None:
    role, target = sys.argv[1], sys.argv[2]
    if target == 'rehearsal':
        # A rehearsal runs the fake-provider worker from tools/rehearsal/rehearse.py; this launcher would start the
        # real worker with real keys on the copy.
        raise SystemExit('a rehearsal is started by tools/rehearsal/rehearse.py up, never by launch.py')
    if role not in ('api', 'worker', 'tunnel') or target != 'live':
        raise SystemExit('usage: launch.py api|worker|tunnel live')
    os.umask(0o077)
    tmp = STUDIO / 'tmp'
    tmp.mkdir(mode=0o700, exist_ok=True)
    # MediaStore probes uploads and downloads with a bare `ffprobe`, so the resolved FFmpeg directory must be on PATH.
    base = {'PATH': FFMPEG_BIN + ':/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(STUDIO), 'TMPDIR': str(tmp),
            'LANG': 'en_US.UTF-8'}
    if role == 'tunnel':
        token = STUDIO / 'env' / 'tunnel.token'
        private_file(token)
        os.execve(CLOUDFLARED, [CLOUDFLARED, 'tunnel', '--no-autoupdate', 'run', '--token-file', str(token)], base)
    run = current(target)
    code_dir = run['code_dir']
    if Path(code_dir).name.startswith('release-'):
        # An old release tree (rollback): its own launcher, env files and configs, exactly as before the lean platform.
        os.execve(str(PY), [str(PY), str(OLD_LAUNCHER), role, target],
                  {**base, 'MVGP_RUNTIME': code_dir})
    secrets = json.loads(private_file(STUDIO / 'env' / f'{target}-{role}.json'))
    env = lean_env(role, target, secrets, code_dir, base)
    os.chdir(code_dir)
    if role == 'api':
        port = env.pop('MVGP_PORT')
        env['MVGP_API_CONFIG'] = run['api_config']
        os.execve(str(PY), [str(PY), '-m', 'uvicorn', 'production.server:app_factory', '--factory', '--host', '127.0.0.1',
                            '--port', port, '--no-proxy-headers', '--timeout-graceful-shutdown', '120'], env)
    os.execve(str(PY), [str(PY), '-m', 'production.worker', '--config', run['worker_config']], env)


if __name__ == '__main__':
    main()
