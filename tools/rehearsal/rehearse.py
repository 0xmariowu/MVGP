"""Rehearse a lean code dir on a restored copy of live, spending nothing.

  <studio>/venv/bin/python tools/rehearsal/rehearse.py prepare --studio <studio> --code-dir <dir> [--backup <dir>]
  <studio>/venv/bin/python tools/rehearsal/rehearse.py up|down --studio <studio>

`prepare` moves any earlier rehearsal state aside, restores the latest backup (or the one named) into
<studio>/rehearsal/state, writes the configs the code dir's own `studio/migrate_config.py` makes from the backup's
live configs (or uses init configs and an empty store when no backup exists), then points them at the copy: the API on https://127.0.0.1:8812 (a self-signed certificate), the
owner signed in by a rehearsal Access identity whose key and JWKS live in <studio>/rehearsal/owner, the worker
following every project. It issues the agent, worker and film credentials in the copy, the way the operator does.
`up` starts the API from the code dir and `tools/rehearsal/worker.py` (the real worker with fake fal, apilio, reader
and result hosts); `down` stops exactly those processes. Every path it writes is under <studio>/rehearsal.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get('MVGP_REHEARSAL_PORT') or 8812)
ORIGIN = f'https://127.0.0.1:{PORT}'
ISSUER = 'https://rehearsal.cloudflareaccess.com'
AUDIENCE = 'e' * 64
SUBJECT = 'rehearsal-owner'
KID = 'rehearsal-owner-key'


class Paths:
    def __init__(self, studio: Path) -> None:
        self.studio = Path(studio).resolve()
        self.root = self.studio / 'rehearsal'
        self.state = self.root / 'state'
        self.database = self.state / 'metadata.sqlite'
        self.config = self.root / 'config'
        self.owner = self.root / 'owner'
        self.tls = self.root / 'tls'
        self.current = self.root / 'current.json'
        self.run = self.root / 'run.json'
        self.media = self.root / 'fake-media'

    def inside(self, path: Path) -> Path:
        """Refuse anything outside <studio>/rehearsal: the rehearsal never writes or opens live."""
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise SystemExit(f'refusing: {path} is outside {self.root}')
        return resolved


def private_json(path: Path, value: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=1, ensure_ascii=False)


def latest_backup(studio: Path) -> Path | None:
    candidates = sorted((p for p in (studio / 'backup').glob('*') if (p / 'metadata.sqlite').exists()),
                        key=lambda p: p.stat().st_mtime)
    if not candidates:
        return None
    return candidates[-1]


def restore(p: Paths, backup: Path | None) -> None:
    if p.state.exists():
        aside = p.inside(p.root / 'old' / time.strftime('state-%Y%m%dT%H%M%S'))
        aside.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.state.rename(aside)
    p.inside(p.state).mkdir(mode=0o700, parents=True)
    if backup is None:
        # issue_tokens initializes this empty database with the code dir's Store schema.
        p.inside(p.database).touch(mode=0o600)
        return
    source = sqlite3.connect(f'file:{backup / "metadata.sqlite"}?mode=ro', uri=True)
    target = sqlite3.connect(p.inside(p.database))
    source.backup(target)
    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise SystemExit('the restored copy fails quick_check')
    target.close()
    source.close()
    p.database.chmod(0o600)
    for part in ('media', 'review-payloads'):
        if (backup / 'state-live' / part).exists():
            subprocess.run(['cp', '-cRp', str(backup / 'state-live' / part), str(p.inside(p.state / part))], check=True)


def owner_identity(p: Paths) -> Path:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    p.inside(p.owner).mkdir(mode=0o700, parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(p.owner / 'key.pem', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(pem)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    private_json(p.owner / 'jwks.json', {'keys': [{**public, 'kid': KID, 'alg': 'RS256', 'use': 'sig'}]})
    return p.owner / 'jwks.json'


def owner_jwt(studio: Path, **changes: Any) -> str:
    """A signed rehearsal Access identity, as Cloudflare Access would put on every desk request."""
    import jwt
    p = Paths(studio)
    now = int(time.time())
    claims = {'iss': ISSUER, 'aud': [AUDIENCE], 'sub': SUBJECT, 'email': 'owner@rehearsal.test', 'type': 'app',
              'iat': now - 5, 'exp': now + 3600, **changes}
    return jwt.encode(claims, (p.owner / 'key.pem').read_bytes(), algorithm='RS256', headers={'kid': KID})


def certificate(p: Paths) -> None:
    p.inside(p.tls).mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30', '-subj', '/CN=127.0.0.1',
                    '-keyout', str(p.tls / 'key.pem'), '-out', str(p.tls / 'cert.pem')],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (p.tls / 'key.pem').chmod(0o600)


HF_ENVELOPE = {'budget_key': 'hf_owner', 'ceiling': 8000, 'unit': 'hf_credit'}  # per new film


def install_fake_hf(p: Paths, code_dir: Path) -> dict[str, Any]:
    """The fake Higgsfield CLI (tools/rehearsal/fake_hf.py behind a native launcher, as the adapter pins a native
    binary) in <studio>/rehearsal/fake-hf, answering `model get` with the code dir's pinned capability. It spends
    nothing and reaches nothing; its HOME is its own, never the live Higgsfield sign-in."""
    target = p.inside(p.root / 'fake-hf')
    (target / 'capabilities').mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    shutil.copy(here / 'fake_hf.py', target / 'fake_hf.py')
    (target / 'version.txt').write_text('rehearsal-hf 0.0.3 (fake, spends nothing)\n')
    runtime = json.loads((code_dir / 'production/config/runtime.json').read_text())
    roles = {'seedance_2_5': 'hf_video_capability'}
    for job_type, role in roles.items():
        (target / 'capabilities' / f'{job_type}.json').write_text(json.dumps(runtime['capabilities'][role]))
    binary = target / 'fake-hf'
    subprocess.run(['cc', f'-DPYTHON="{sys.executable}"', f'-DSCRIPT="{target / "fake_hf.py"}"', '-o', str(binary),
                    str(here / 'fake-hf.c')], check=True)
    home = target / 'hf-home'
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    for leftover in ('jobs.json', 'calls.log', 'creates.jsonl'):
        if (home / leftover).exists():
            (home / leftover).rename(home / f'{leftover}.{int(time.time())}')
    import hashlib
    return {'native_path': str(binary), 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'version': (target / 'version.txt').read_text().strip(), 'credential_home': str(home),
            'service_uid': os.getuid(), 'capability_roles': roles, 'timeout': 120}


def configs(p: Paths, backup: Path | None, code_dir: Path) -> None:
    source = (p.studio / 'config/live' if backup is None else
              backup / 'config-current' if (backup / 'config-current' / 'api.json').exists() else backup / 'config-live')
    api, worker = json.loads((source / 'api.json').read_text()), json.loads((source / 'worker.json').read_text())
    work = p.inside(p.root / 'config-migrated')
    if work.exists():
        shutil.rmtree(work)  # this rehearsal's own scratch output
    if 'personal' in api:  # the release-97 shape: the code dir's own migration makes the lean shape
        subprocess.run([sys.executable, str(code_dir / 'studio/migrate_config.py'), '--api', str(source / 'api.json'),
                        '--worker', str(source / 'worker.json'), '--db', str(p.database), '--out-dir', str(work)],
                       check=True, stdout=subprocess.DEVNULL)
        api, worker = json.loads((work / 'api.json').read_text()), json.loads((work / 'worker.json').read_text())
    storage = {'mode': 'local', 'database': str(p.database), 'media_root': str(p.state / 'media')}
    # Live runs the owner's desk on localhost with no login (local_owner); the rehearsal signs the owner
    # in through its own Access identity on https, so the local desk is off here (the two cannot be on together).
    api.update(storage=storage, public_origin=ORIGIN, local_owner=False,
               owner={'issuer': ISSUER, 'audience': AUDIENCE, 'subjects': [SUBJECT], 'jwks_file': str(owner_identity(p))})
    worker.update(storage=storage, public_origin=ORIGIN, project_ids=[], live_enabled=True,
                  worker_token_env='MVGP_WORKER_TOKEN', film_token_env='MVGP_FILM_TOKEN')
    # Higgsfield is the shooting route; the copy shoots on the fake CLI, never the live one,
    # and a new film gets its Higgsfield envelope as live will.
    worker['hf'] = install_fake_hf(p, code_dir)
    api['default_envelopes'] = [*(e for e in api.get('default_envelopes', []) if e.get('budget_key') != 'hf_owner'), HF_ENVELOPE]
    p.inside(p.config).mkdir(mode=0o700, parents=True, exist_ok=True)
    private_json(p.config / 'api.json', api)
    private_json(p.config / 'worker.json', worker)
    env = {**os.environ, 'PYTHONPATH': str(code_dir)}
    subprocess.run([sys.executable, '-c', 'import json,sys\nfrom production.server import ServerConfiguration\n'
                    'from production.worker import WorkerConfiguration\n'
                    'ServerConfiguration.model_validate(json.load(open(sys.argv[1])))\n'
                    'WorkerConfiguration.model_validate(json.load(open(sys.argv[2])))',
                    str(p.config / 'api.json'), str(p.config / 'worker.json')], check=True, env=env, cwd=code_dir)


def issue_tokens(p: Paths, code_dir: Path) -> None:
    """Operator step, as for live: an agent that may create projects; a worker and a film agent for every project."""
    sys.path.insert(0, str(code_dir))
    from production.auth import AuthService
    from production.store import Store
    auth = AuthService(Store(p.database), ORIGIN)
    credentials = p.inside(p.config / 'credentials')
    credentials.mkdir(mode=0o700, exist_ok=True)
    tokens = {'maker.token': auth.provision_token('rehearsal_agent', 'agent', [], 86400, allow_create_project=True),
              'worker.token': auth.provision_token('rehearsal_worker', 'worker', [], 86400, all_projects=True),
              'film.token': auth.provision_token('rehearsal_film', 'agent', [], 86400, all_projects=True)}
    for name, secret in tokens.items():
        fd = os.open(credentials / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(secret)


def prepare(studio: Path, code_dir: Path, backup: Path | None) -> dict[str, Any]:
    p = Paths(studio)
    if running(p):
        raise SystemExit('refusing: a rehearsal is running; stop it first (down)')
    code_dir = Path(code_dir).resolve()
    if not (code_dir / 'production/config/runtime.json').exists():
        raise SystemExit(f'{code_dir} is not a lean code dir (no production/config/runtime.json)')
    backup = Path(backup).resolve() if backup else latest_backup(p.studio)
    if backup is None and not (p.studio / 'init.json').is_file():
        raise SystemExit('no backup: run studio/init.sh before preparing an empty rehearsal')
    p.root.mkdir(mode=0o700, exist_ok=True)
    restore(p, backup)
    configs(p, backup, code_dir)
    issue_tokens(p, code_dir)
    certificate(p)
    private_json(p.current, {'code_dir': str(code_dir), 'api_config': str(p.config / 'api.json'),
                             'worker_config': str(p.config / 'worker.json')})
    return {'backup': str(backup) if backup else None, 'code_dir': str(code_dir), 'origin': ORIGIN, 'state': str(p.state)}


def running(p: Paths) -> bool:
    if not p.run.exists():
        return False
    for pid in json.loads(p.run.read_text()).get('pids', {}).values():
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            continue
    return False


def environment(p: Paths, code_dir: Path, worker_config: dict[str, Any]) -> dict[str, str]:
    ffmpeg = str(Path(worker_config['ffmpeg_path']).parent)
    (p.root / 'tmp').mkdir(mode=0o700, exist_ok=True)
    return {'PATH': f'{ffmpeg}:/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(p.root), 'TMPDIR': str(p.root / 'tmp'),
            'LANG': 'en_US.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': f'{code_dir}:{REPO}', 'PYTHONUNBUFFERED': '1'}


def record_reshoot_cutoff(database: Path, worker_config: Path, code_dir: Path) -> int | None:
    """as the deploy does (studio/reshoot_cutoff.py), the highest event number of the copy
    goes into the worker config before it starts, so only 再拍一批 answers made during the rehearsal re-fire. A code
    dir predating the reshoot cutoff has no such field and gets none."""
    if 'reshoot_after_event' not in (Path(code_dir) / 'production/worker.py').read_text():
        return None
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from studio.reshoot_cutoff import record
    return record(database, worker_config)


def up(studio: Path, *, complete_delay: float = 5.0) -> dict[str, int]:
    p = Paths(studio)
    if running(p):
        raise SystemExit('refusing: an earlier rehearsal is still running; stop it first (down)')
    run = json.loads(p.current.read_text())
    code_dir = Path(run['code_dir'])
    worker_config = json.loads(Path(run['worker_config']).read_text())
    for config in (run['api_config'], run['worker_config']):
        if Path(json.loads(Path(config).read_text())['storage']['database']).resolve() != p.database.resolve():
            raise SystemExit('refusing: a rehearsal config names another database')
    record_reshoot_cutoff(p.database, p.inside(Path(run['worker_config'])), code_dir)
    worker_config = json.loads(Path(run['worker_config']).read_text())
    env = environment(p, code_dir, worker_config)
    logs = p.inside(p.root / 'logs')
    logs.mkdir(mode=0o700, exist_ok=True)
    python = str(p.studio / 'venv/bin/python')
    api = subprocess.Popen([python, '-m', 'uvicorn', 'production.server:app_factory', '--factory', '--host', '127.0.0.1',
                            '--port', str(PORT), '--no-proxy-headers', '--ssl-keyfile', str(p.tls / 'key.pem'),
                            '--ssl-certfile', str(p.tls / 'cert.pem')], cwd=code_dir,
                           env={**env, 'MVGP_API_CONFIG': run['api_config']},
                           stdout=open(logs / 'api.log', 'a'), stderr=subprocess.STDOUT, start_new_session=True)
    credentials = p.config / 'credentials'
    worker = subprocess.Popen([python, str(REPO / 'tools/rehearsal/worker.py'), '--config', run['worker_config']], cwd=code_dir,
                              env={**env, 'MVGP_WORKER_TOKEN': (credentials / 'worker.token').read_text().strip(),
                                   'MVGP_FILM_TOKEN': (credentials / 'film.token').read_text().strip(),
                                   # The fakes never read them; the adapters want them set.
                                   'FAL_AI_TOKEN': 'rehearsal', 'APILIO_AI_KEY': 'rehearsal',
                                   'MVGP_REHEARSAL_MEDIA': str(p.media), 'MVGP_REHEARSAL_FAL_LOG': str(p.root / 'fal-requests.jsonl'),
                                   'MVGP_COMPLETE_DELAY': str(complete_delay)},
                              stdout=open(logs / 'worker.log', 'a'), stderr=subprocess.STDOUT, start_new_session=True)
    pids = {'api': api.pid, 'worker': worker.pid}
    private_json(p.run, {'pids': pids})
    import ssl
    context = ssl._create_unverified_context()  # the rehearsal's own self-signed certificate
    for _ in range(120):
        try:
            urllib.request.urlopen(f'{ORIGIN}/health', timeout=2, context=context)
            break
        except urllib.error.HTTPError:
            break
        except Exception:  # noqa: BLE001 -- not up yet
            time.sleep(0.5)
    return pids


def down(studio: Path) -> None:
    p = Paths(studio)
    if not p.run.exists():
        return
    for pid in json.loads(p.run.read_text()).get('pids', {}).values():
        try:
            os.killpg(pid, signal.SIGTERM)  # our own session leaders, by exact pid
        except (ProcessLookupError, PermissionError):
            pass
    private_json(p.run, {'pids': {}})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('command', choices=('prepare', 'up', 'down'))
    parser.add_argument('--studio', type=Path, required=True)
    parser.add_argument('--code-dir', type=Path)
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        if args.code_dir is None:
            raise SystemExit('prepare needs --code-dir')
        print(json.dumps(prepare(args.studio, args.code_dir, args.backup)))
    elif args.command == 'up':
        print(json.dumps(up(args.studio)))
    else:
        down(args.studio)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
