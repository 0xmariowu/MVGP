"""Create a private first studio; an initialized studio is left untouched on repeat runs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from production.operations import IssueRequest, OperatorConfig
from production.server import ServerConfiguration
from production.worker import WorkerConfiguration
from studio.launch import LEAN_KEYS

REPO = Path(__file__).resolve().parents[1]


def write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def sample_configs(studio: Path, port: int) -> dict[str, dict]:
    """Use the service models; credentials and provider sign-in are never sample data."""
    origin = f'http://localhost:{port}'
    storage = {'mode': 'local', 'database': str(studio / 'state/live/metadata.sqlite'),
               'media_root': str(studio / 'state/live/media')}
    binaries = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe')}
    if not all(binaries.values()):
        raise SystemExit('init: install ffmpeg and ffprobe on PATH first')
    api = ServerConfiguration(storage=storage, public_origin=origin, local_owner=True, live_enabled=True,
        default_envelopes=[{'budget_key': 'hf_owner', 'unit': 'hf_credit', 'ceiling': 8000},
                           {'budget_key': 'fal_owner', 'unit': 'usd_micro', 'ceiling': 270000000},
                           {'budget_key': 'apilio_owner_10466', 'unit': 'apilio_quota', 'ceiling': 5000000}])
    worker = WorkerConfiguration(
        storage=storage, public_origin=origin, worker_token_env='MVGP_WORKER_TOKEN',
        film_token_env='MVGP_FILM_TOKEN', live_enabled=True, reader_enabled=True, cuts_enabled=True,
        ffmpeg_path=str(Path(binaries['ffmpeg']).resolve()), ffprobe_path=str(Path(binaries['ffprobe']).resolve()), lease_seconds=600,
        fal={'capability_roles': {'fal_seedance_2_5': 'fal_video_capability',
                                'fal_seedance_2_5_complete': 'fal_complete_capability'}},
        apilio_images={'capability_roles': {'apilio_gpt_image_2_5': 'apilio_image_capability'}},
        download_hosts=['v3.fal.media', 'v3b.fal.media', 'webstatic.aiproxy.vip', 'd8j0ntlcm91z4.cloudfront.net'])
    operator = OperatorConfig(database=storage['database'], public_origin=origin, operator_id='owner_operator',
                              credential_directory=str(studio / 'config/live/credentials'))
    return {name: model.model_dump(mode='json', exclude_none=True)
            for name, model in (('api', api), ('worker', worker), ('operator', operator))}


def initialize(studio: Path, port: int = 8811) -> bool:
    studio = studio.expanduser().resolve()
    if not 1 <= port <= 65535:
        raise SystemExit('init: MVGP_PORT must be between 1 and 65535')
    if (studio / 'init.json').is_file():
        print('init: already initialized; existing files preserved')
        return False
    configs = sample_configs(studio, port)
    # A partial/foreign install must be repaired explicitly, never silently rewritten or re-keyed.
    files = ['current.json', 'launch.py', 'ops.sh', 'run-service.sh', 'state/live/metadata.sqlite',
             *[f'config/live/{name}.json' for name in configs],
             *[f'env/live-{name}.json' for name in ('api', 'worker', 'ledger')],
             *[f'config/live/credentials/{name}.token' for name in ('owner-agent', 'worker', 'film')]]
    conflicts = [name for name in files if os.path.lexists(studio / name)]
    if conflicts:
        raise SystemExit('init: refusing to overwrite existing files: ' + ', '.join(conflicts))
    os.umask(0o077)
    for folder in ('', 'runtime', 'config/live/credentials', 'env', 'state/live/media', 'state/live/review-payloads',
                   'bin', 'hf-home', 'backup', 'evidence', 'logs', 'tmp'):
        path = studio / folder
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
            raise SystemExit(f'init: {path} must be a private directory owned by this user')
    # Optional native CLI provisioning. Absence leaves the desk/rehearsal usable; no alternate video route is selected.
    native = os.environ.get('MVGP_HF_BIN')
    if native:
        from production.provider_hf import NATIVE_HEADERS
        raw = Path(native).read_bytes()
        if raw[:4] not in NATIVE_HEADERS or len(raw) > 67108864:
            raise SystemExit('init: MVGP_HF_BIN must name the native Higgsfield executable, not its Node shim')
        target = studio / 'bin/hf'
        write_new(target, raw, 0o700)
        version = subprocess.check_output([str(target), '--version'], timeout=30,
                                          env={'HOME': str(studio / 'hf-home'), 'PATH': '/usr/bin:/bin'}).decode().strip()
        configs['worker']['hf'] = {'native_path': str(target), 'sha256': hashlib.sha256(raw).hexdigest(),
            'version': version, 'credential_home': str(studio / 'hf-home'), 'service_uid': os.getuid(),
            'capability_roles': {'seedance_2_5': 'hf_video_capability'}, 'timeout': 120}
    for name, value in configs.items():
        write_new(studio / f'config/live/{name}.json', json.dumps(value, indent=2).encode())
    for name in ('launch.py', 'ops.sh'):
        write_new(studio / name, (REPO / 'studio' / name).read_bytes(), 0o700 if name.endswith('.sh') else 0o600)
    write_new(studio / 'run-service.sh', b'''#!/bin/bash
set -euo pipefail
S=$(cd "$(dirname "$0")" && pwd -P)
export MVGP_STUDIO=$S
exec "$S/venv/bin/python" "$S/launch.py" "$@"
''', 0o700)
    # issue initializes the Store and writes each secret only to a private credential file.
    for name, role, scope in (('owner-agent', 'agent', {'allow_create_project': True}),
                              ('worker', 'worker', {'all_projects': True}), ('film', 'agent', {'all_projects': True})):
        request = IssueRequest(actor_id=name.replace('-', '_'), role=role, project_ids=[], ttl_seconds=90*86400,
                               output_name=f'{name}.token', **scope)
        subprocess.run([sys.executable, '-m', 'production.operations', '--config', str(studio / 'config/live/operator.json'),
                        'issue', '--input', '-'], input=request.model_dump_json(), text=True, check=True,
                       stdout=subprocess.DEVNULL, cwd=REPO)
    for role in ('api', 'worker'):
        value = dict.fromkeys(LEAN_KEYS[role], '')
        if role == 'api':
            value['MVGP_PORT'] = str(port)
        else:
            for key, name in (('MVGP_WORKER_TOKEN', 'worker'), ('MVGP_FILM_TOKEN', 'film')):
                value[key] = (studio / f'config/live/credentials/{name}.token').read_text().strip()
        write_new(studio / f'env/live-{role}.json', json.dumps(value, indent=2).encode())
    write_new(studio / 'env/live-ledger.json', json.dumps({'FAL_AI_ADMIN_TOKEN': ''}).encode())
    write_new(studio / 'init.json', json.dumps({'schema_version': 1, 'port': port}).encode())
    print(f'init: ready at {studio}; credentials issued (90 days); fill provider keys and review sample spending envelopes')
    return True


def main() -> None:
    initialize(Path(os.environ.get('MVGP_STUDIO') or '/Users/Shared/mvgp-studio'), int(os.environ.get('MVGP_PORT') or 8811))


if __name__ == '__main__':
    main()
