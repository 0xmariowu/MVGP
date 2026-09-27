"""Thin MVGP HTTP client. The service alone owns production truth and rules."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Never, Self, TextIO
from urllib.parse import urlsplit

import httpx

INPUT_LIMIT = 1048576
RESPONSE_LIMIT = 33554432
MEDIA_LIMIT = 2_147_483_648
# Cloud cold startup is bounded at 60 seconds, before the handler roundtrip.
REQUEST_TIMEOUT_SECONDS = 180
JOB_KINDS = {'job', 'composition-job', 'review-task'}
TERMINAL = {'succeeded', 'completed', 'failed', 'cancelled', 'unknown'}
# Fixed client vocabulary, never a generic path or operation supplied by an author.
MUTATIONS = {
    'create-project': ('POST', '/v1/projects'),
    'draft': ('POST', '/v1/projects/{project}/artifacts'),
    'revise': ('PUT', '/v1/projects/{project}/artifacts/{object}'),
    'select-method': ('POST', '/v1/projects/{project}/method-selections'),
    'prepare': ('POST', '/v1/projects/{project}/candidates'),
    'submit': ('POST', '/v1/projects/{project}/submissions'),
    'observe': ('POST', '/v1/projects/{project}/observations'),
    'create-batch': ('POST', '/v1/projects/{project}/batches'),
    'select-take': ('POST', '/v1/projects/{project}/take-selections'),
    'batch-select': ('POST', '/v1/projects/{project}/batches/{object}/selections'),
    'cut': ('POST', '/v1/projects/{project}/cuts'),
    'render-cut': ('POST', '/v1/projects/{project}/cuts/render'),
    'feedback': ('POST', '/v1/projects/{project}/feedback'),
    'report': ('POST', '/v1/projects/{project}/reports'),
    'patch': ('POST', '/v1/projects/{project}/patches'),
    'request-decision': ('POST', '/v1/projects/{project}/decision-requests'),
    'reopen': ('POST', '/v1/projects/{project}/reopens'),
}
FOLDER_COMMANDS = ('open', 'push', 'image', 'quote', 'shoot', 'pull')
EXIT = {'invalid_input': 2, 'configuration_error': 2, 'unauthorized': 3, 'forbidden': 3,
        'not_found': 4, 'revision_conflict': 5, 'idempotency_conflict': 5, 'stale_input': 5,
        'transport_error': 7, 'poll_timeout': 8, 'invalid_response': 9}


class CLIError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse's default error includes user arguments, possibly secrets.
        raise CLIError('invalid_input', 'Invalid command arguments; use --help for the supported syntax')


def identifier(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise CLIError('invalid_input', 'Object, project and document identifiers must be single safe identifiers')
    return value


def positive(value: str) -> int:
    try:
        result = int(value)
        if result < 1:
            raise ValueError
        return result
    except ValueError:
        raise CLIError('invalid_input', 'Revision must be a positive integer') from None


def bounded_seconds(value: str) -> float:
    try:
        result = float(value)
        if not math.isfinite(result) or not 0 <= result <= 300:
            raise ValueError
        return result
    except ValueError:
        raise CLIError('invalid_input', 'Polling duration must be finite and between zero and 300 seconds') from None


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read_json(source: str, stdin: TextIO) -> dict[str, Any]:
    try:
        if source == '-':
            data = stdin.read(INPUT_LIMIT + 1)
            raw = data.encode('utf-8')
        else:
            with Path(source).open('rb') as stream:
                raw = stream.read(INPUT_LIMIT + 1)
        if len(raw) > INPUT_LIMIT:
            raise ValueError('Input bound')
        value = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
        if not isinstance(value, dict):
            raise TypeError('Expected an object')
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return value
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise CLIError('invalid_input', 'Input must be a readable, bounded UTF-8 JSON object without duplicate keys') from None


def configuration(environ: Mapping[str, str]) -> tuple[str, str]:
    base, token = environ.get('MVGP_URL', ''), environ.get('MVGP_TOKEN', '')
    try:
        parts = urlsplit(base)
        host = parts.hostname
        loopback = host == 'localhost'
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parts.username is not None or parts.password is not None
                or parts.path not in ('', '/') or parts.query or parts.fragment
                or parts.scheme not in ('http', 'https') or (parts.scheme == 'http' and not loopback)
                or parts.port == 0 or any(char.isspace() for char in base)):
            raise ValueError
        if not token or len(token) > 8192 or not token.isascii() or any(char.isspace() for char in token):
            raise ValueError
    except ValueError:
        raise CLIError('configuration_error', 'Set MVGP_URL to an HTTPS or loopback service origin and MVGP_TOKEN to a scoped token') from None
    return base.rstrip('/'), token


def access_configuration(environ: Mapping[str, str], base: str) -> tuple[str, str] | None:
    """Optional edge identity for an agent: the Access service credentials, bound to one HTTPS origin; no platform
    authority. A person's Access login is refused: the owner's login opens his desk, so an
    agent must never carry it."""
    if environ.get('MVGP_ACCESS_TOKEN', ''):
        raise CLIError('configuration_error', "A person's Access login is not an agent credential; use the Access service "
                       'credentials (MVGP_ACCESS_CLIENT_ID and MVGP_ACCESS_CLIENT_SECRET)')
    names = ('MVGP_ACCESS_ORIGIN', 'MVGP_ACCESS_CLIENT_ID', 'MVGP_ACCESS_CLIENT_SECRET')
    values = [environ.get(name, '') for name in names]
    if not any(values):
        return None
    origin, client_id, secret = values
    if (origin != base or not base.startswith('https://')
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,8192}', value) for value in (client_id, secret))):
        raise CLIError('configuration_error', 'Access requires paired credentials and MVGP_ACCESS_ORIGIN matching the exact HTTPS MVGP_URL')
    return client_id, secret


class Client:
    def __init__(self, base: str, token: str, *, transport: httpx.BaseTransport | None = None,
                 access: tuple[str, str] | None = None) -> None:
        headers = {'Authorization': 'Bearer ' + token}
        if access is not None:
            headers.update({'CF-Access-Client-Id': access[0], 'CF-Access-Client-Secret': access[1]})
        self.http = httpx.Client(base_url=base, headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False, transport=transport)

    def download(self, path: str, dest: Path, *, params: dict[str, Any] | None = None, sha256: str) -> None:
        """Stream one media revision to `dest`, bounded, and keep it only when its SHA-256 is the one expected."""
        if not path.startswith('/v1/projects/') or '..' in path:
            raise CLIError('configuration_error', 'Downloads are media paths on the configured origin')
        hashed, size = hashlib.sha256(), 0
        try:
            with self.http.stream('GET', path, params=params) as response, dest.open('wb') as out:
                if response.status_code != 200:
                    raise CLIError('invalid_response', f'Media download answered HTTP {response.status_code}')
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MEDIA_LIMIT:
                        raise CLIError('invalid_response', 'Media exceeds the client byte bound')
                    hashed.update(chunk)
                    out.write(chunk)
        except httpx.HTTPError:
            dest.unlink(missing_ok=True)
            raise CLIError('transport_error', 'Media download failed') from None
        except CLIError:
            dest.unlink(missing_ok=True)
            raise
        if hashed.hexdigest() != sha256:
            dest.unlink(missing_ok=True)
            raise CLIError('invalid_response', 'Downloaded media differs from its recorded SHA-256')

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.http.close()

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None, timeout: float = REQUEST_TIMEOUT_SECONDS,
                content: Iterable[bytes] | None = None, headers: dict[str, str] | None = None) -> Any:
        if not path.startswith('/') or path.startswith('//') or '\\' in path or any(ord(c) < 32 for c in path):
            raise CLIError('configuration_error', 'Platform requests require a path on the configured origin')
        try:
            with self.http.stream(method, path, json=body, params=params, timeout=timeout, content=content, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise CLIError('transport_error', 'Service redirects are refused; verify MVGP_URL')
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > RESPONSE_LIMIT:
                        raise CLIError('invalid_response', 'Service response exceeds the client byte bound')
                    raw.extend(chunk)
                try:
                    value = json.loads(raw, object_pairs_hook=_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
                    if not isinstance(value, (dict, list)):
                        raise TypeError
                    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise CLIError('invalid_response', 'Service did not return a bounded JSON object or list') from None
                if response.is_error:
                    if not isinstance(value, dict) or not isinstance(value.get('code'), str):
                        raise CLIError('invalid_response', 'Service error does not match its published error format')
                    code = value['code']
                    if not re.fullmatch(r'[a-z][a-z_]{0,63}', code):
                        raise CLIError('invalid_response', 'Service returned an invalid error code')
                    # Only platform error fields, never an HTTP request or raw body.
                    details = {key: value[key] for key in ('field', 'object_ref', 'rule_id', 'source', 'current_revision', 'repair')
                               if key in value and value[key] is not None}
                    message = value.get('message')
                    if not isinstance(message, str) or len(message) > 4000:
                        message = 'The platform rejected this request'
                    raise CLIError(code, message, details=details)
                return value
        except httpx.HTTPError:
            raise CLIError('transport_error', 'Platform request failed; no automatic retry was attempted') from None


def parser() -> Parser:
    root = Parser(prog='mvgp', description='Use MVGP through the platform only. Configure MVGP_URL and MVGP_TOKEN in the environment.')
    commands = root.add_subparsers(dest='command', required=True, parser_class=Parser)
    for name in ('discovery', 'projects'):
        commands.add_parser(name)
    for name in ('project', 'jobs', 'methods', 'tree', 'context', 'inspect', 'candidate-inspect', 'reference', 'project-page'):
        cmd = commands.add_parser(name)
        cmd.add_argument('project', type=identifier)
        if name in ('context', 'inspect', 'candidate-inspect', 'reference'):
            cmd.add_argument('--input', required=True, help='JSON file or - for stdin; schema comes from discovery')
        if name == 'tree':
            cmd.add_argument('--parent', default='')
    for name in ('read', 'history', 'status', 'batch'):
        cmd = commands.add_parser(name)
        cmd.add_argument('project', type=identifier)
        cmd.add_argument('object', type=identifier)
        if name == 'read':
            cmd.add_argument('--revision', type=positive)
        if name == 'status':
            cmd.add_argument('--wait-seconds', type=bounded_seconds, default=0)
            cmd.add_argument('--interval', type=bounded_seconds, default=2)
    manuals = commands.add_parser('playbook', help='Fetch the writer manuals into a folder; the platform records the version you took')
    manuals.add_argument('project', type=identifier)
    manuals.add_argument('--dir', required=True, help='Folder to write the manuals into (created if missing)')
    doc = commands.add_parser('document')
    doc.add_argument('project', type=identifier)
    doc.add_argument('role', type=identifier)
    resolve = commands.add_parser('resolve', help='Resolve an exact copy-reference payload from the viewer')
    resolve.add_argument('--input', required=True)
    for name, (_method, route) in MUTATIONS.items():
        cmd = commands.add_parser(name, help='Submit authored JSON; the platform checks permissions, versions and rules')
        if '{project}' in route:
            cmd.add_argument('project', type=identifier)
        if '{object}' in route:
            cmd.add_argument('object', type=identifier)
        cmd.add_argument('--input', required=True, help='Exact request JSON file or - for stdin; no generated idempotency keys')
    # the HF-shaped project folder (production/FOLDER.md, production/folder_cli.py).
    opened = commands.add_parser('open', help='Create or resume the project of an HF-shaped folder; fetch the manuals; make the skeleton')
    opened.add_argument('dir')
    opened.add_argument('--branch', choices=['original', 'recreation'], default='original')
    pushed = commands.add_parser('push', help='Send the folder units that changed since the last push; nothing else')
    pushed.add_argument('dir')
    imaged = commands.add_parser('image', help="Push, then make each asset's image and put it in ASSETS/<KIND>/<tag>.png")
    imaged.add_argument('dir')
    imaged.add_argument('tags', nargs='+')
    imaged.add_argument('--wait-seconds', type=bounded_seconds, default=300)
    for name, text in (('quote', 'Push, then what shooting these shots (default: all) would cost; nothing is spent'),
                       ('shoot', "Push, then order four takes of each named shot or scene, with the reviewer's notes")):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument('dir')
        cmd.add_argument('shots', nargs='*', help='S01-010, 01:010, or a scene number such as 01')
        cmd.add_argument('--takes', type=int, choices=[1, 2, 3, 4], default=4)
        if name == 'shoot':
            cmd.add_argument('--review', help="The fresh reviewer's notes: JSON {reviewer, notes: [{line, note, answer, shot?}]}")
    pulled = commands.add_parser('pull', help="Write log.md from the platform and download the owner's picks")
    pulled.add_argument('dir')
    pulled.add_argument('--all', action='store_true', help='Download every take, not only the picks')
    upload_cmd = commands.add_parser('upload', help='Stream one regular file with its exact published upload metadata')
    upload_cmd.add_argument('project', type=identifier)
    upload_cmd.add_argument('file')
    upload_cmd.add_argument('--input', required=True, help='UploadRequest JSON file or - for stdin, including length and SHA-256')
    return root


@contextmanager
def local_file(filename: str) -> Iterator[BinaryIO]:
    """Open every path component without following links; reject devices/FIFOs."""
    path = Path(filename).absolute()
    if '..' in path.parts or len(path.parts) > 128:
        raise CLIError('invalid_input', 'Upload path must name a regular file without traversal or symbolic links')
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CLIError('invalid_input', 'Upload source must be a regular file')
            yield stream
    except OSError:
        raise CLIError('invalid_input', 'Upload file is unavailable or contains a symbolic link') from None
    finally:
        os.close(directory)


def _identity(stream: BinaryIO) -> tuple[int, int, int, int, int]:
    value = os.fstat(stream.fileno())
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def upload(client: Client, args: argparse.Namespace, metadata: dict[str, Any]) -> Any:
    header = json.dumps(metadata, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    length, digest = metadata.get('byte_length'), metadata.get('sha256')
    if (len(header.encode()) > 16384 or type(length) is not int or length < 1
            or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)):
        raise CLIError('invalid_input', 'Upload metadata requires a bounded header, positive exact length and SHA-256')
    catalog = client.request('GET', '/v1/discovery')
    routes = [item for item in catalog['mutation_routes']
              if item.get('path') == '/v1/projects/{pid}/uploads' and item.get('method') == 'POST']
    if (len(routes) != 1 or type(routes[0].get('max_bytes')) is not int
            or routes[0]['max_bytes'] < 1):
        raise CLIError('invalid_response', 'The platform does not publish a bounded upload route')
    if length > routes[0]['max_bytes']:
        raise CLIError('invalid_input', 'Upload exceeds the published platform byte bound')
    with local_file(args.file) as stream:
        original = _identity(stream)
        if original[2] != length:
            raise CLIError('invalid_input', 'Upload file length differs from the supplied metadata')
        hashed, count = hashlib.sha256(), 0
        while chunk := stream.read(65536):
            count += len(chunk)
            if count > length:
                raise CLIError('invalid_input', 'Upload file grew during verification')
            hashed.update(chunk)
        if count != length or hashed.hexdigest() != digest or _identity(stream) != original:
            raise CLIError('invalid_input', 'Upload content changed or differs from its supplied SHA-256')
        stream.seek(0)
        def chunks() -> Iterator[bytes]:
            if _identity(stream) != original:
                raise CLIError('invalid_input', 'Upload file changed before transmission')
            sent, actual = 0, hashlib.sha256()
            while chunk := stream.read(65536):
                sent += len(chunk)
                if sent > length:
                    raise CLIError('invalid_input', 'Upload file grew during transmission')
                actual.update(chunk)
                yield chunk
            if sent != length or actual.hexdigest() != digest or _identity(stream) != original:
                raise CLIError('invalid_input', 'Upload file changed during transmission; inspect the platform before retrying')
        return client.request('POST', '/v1/projects/' + args.project + '/uploads', content=chunks(),
            headers={'Content-Type': 'application/octet-stream', 'X-MVGP-Upload': header, 'Content-Length': str(length)})


def resolve(client: Client, body: dict[str, Any]) -> dict[str, Any]:
    try:
        pid = identifier(body['project_id'])
        ref = body['object_ref']
        identifier(ref['object_id'])
        if (type(ref['revision']) is not int or ref['revision'] < 1
                or not isinstance(ref['digest'], str) or not re.fullmatch(r'[a-f0-9]{64}', ref['digest'])):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise CLIError('invalid_input', 'Copied references require project_id and an exact object ID, revision and digest') from None
    # Ignore the copied link: only the configured platform is an HTTP authority.
    reference = client.request('POST', f'/v1/projects/{pid}/reference',
        body={'target': ref, 'seconds': body.get('playback_seconds')})
    artifact = client.request('GET', f'/v1/projects/{pid}/artifacts/{ref["object_id"]}', params={'revision': ref['revision']})
    if reference.get('object_ref') != ref or artifact.get('object_ref') != ref:
        raise CLIError('invalid_response', 'Resolved content does not match the exact copied reference')
    return {'reference': reference, 'artifact': artifact}


def playbook(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    """write the manuals the platform handed over, byte-checked."""
    body = client.request('GET', f'/v1/projects/{args.project}/playbook')
    folder = Path(args.dir).absolute()
    if '..' in folder.parts or folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise CLIError('invalid_input', 'The manuals folder must be a plain directory path without traversal or links')
    try:
        version, files = body['version'], body['files']
        checked = []
        for item in files:
            name, text, digest = item['name'], item['text'], item['sha256']
            if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,79}\.md', name) or hashlib.sha256(text.encode()).hexdigest() != digest:
                raise ValueError
            checked.append((name, text, digest))
        if not isinstance(version, str) or not checked:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CLIError('invalid_response', 'The platform returned manuals that do not match their own hashes') from None
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text, digest in checked:
        target = folder / name
        if target.is_symlink() or target.is_dir():
            raise CLIError('invalid_input', 'A manual path in the folder is a symbolic link or a directory')
        target.write_text(text)
        written.append({'name': name, 'path': str(target), 'sha256': digest})
    return {'project_id': args.project, 'playbook_version': version, 'files': written}


def execute(client: Client, args: argparse.Namespace, stdin: TextIO) -> Any:
    command = args.command
    if command in FOLDER_COMMANDS:
        from production import folder_cli
        from production.folder import FolderError
        try:
            if command == 'open':
                return folder_cli.open_project(client, Path(args.dir), branch=args.branch)
            if command == 'push':
                return folder_cli.push(client, Path(args.dir))
            if command == 'image':
                return folder_cli.image(client, Path(args.dir), args.tags, wait_seconds=args.wait_seconds)
            if command == 'quote':
                return folder_cli.quote(client, Path(args.dir), args.shots, takes=args.takes)
            if command == 'shoot':
                return folder_cli.shoot(client, Path(args.dir), args.shots, takes=args.takes,
                                        review=Path(args.review) if args.review else None)
            if command == 'pull':
                return folder_cli.pull(client, Path(args.dir), every=args.all)
        except folder_cli.FolderCommandError as exc:
            raise CLIError(exc.code, exc.message) from None
        except FolderError as exc:
            raise CLIError('invalid_input', 'The folder cannot be read: ' + '; '.join(exc.problems)[:3500]) from None
    if command in MUTATIONS:
        method, route = MUTATIONS[command]
        return client.request(method, route.format(**vars(args)), body=read_json(args.input, stdin))
    if command == 'upload':
        return upload(client, args, read_json(args.input, stdin))
    if command in ('discovery', 'projects'):
        return client.request('GET', '/v1/' + command)
    if command == 'resolve':
        return resolve(client, read_json(args.input, stdin))
    if command == 'playbook':
        return playbook(client, args)
    prefix = '/v1/projects/' + args.project
    if command in ('project', 'jobs'):
        result = client.request('GET', prefix)
        if command == 'jobs':
            return {'project_id': result['project_id'], 'jobs': [obj for obj in result['results'] if obj['kind'] in JOB_KINDS]}
        return result
    if command == 'methods':
        return client.request('GET', prefix + '/methods')
    if command == 'project-page':
        # the owner's project page as data (stage, HF brief, shotlists, version logs).
        return client.request('GET', prefix + '/project-tree')
    if command == 'tree':
        return client.request('GET', prefix + '/tree', params={'parent': args.parent})
    if command == 'document':
        return client.request('GET', prefix + '/references/' + args.role)
    if command == 'batch':
        return client.request('GET', prefix + '/batches/' + args.object)
    if command in ('context', 'inspect', 'candidate-inspect', 'reference'):
        endpoint = '/candidates/inspect' if command == 'candidate-inspect' else '/' + command
        return client.request('POST', prefix + endpoint, body=read_json(args.input, stdin))
    path = prefix + '/artifacts/' + args.object
    if command == 'history':
        return client.request('GET', path + '/history')
    if command == 'read':
        return client.request('GET', path, params={'revision': args.revision} if args.revision else None)
    if command == 'status':
        if args.wait_seconds and not 0.05 <= args.interval <= 60:
            raise CLIError('invalid_input', 'Polling interval must be between 0.05 and 60 seconds')
        deadline = time.monotonic() + args.wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            result = client.request('GET', path, timeout=min(REQUEST_TIMEOUT_SECONDS, max(0.001, remaining)) if args.wait_seconds else REQUEST_TIMEOUT_SECONDS)
            if result.get('kind') not in JOB_KINDS:
                raise CLIError('invalid_input', 'Status polling requires a job, composition job or independent review task')
            if not args.wait_seconds or result.get('status') in TERMINAL:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CLIError('poll_timeout', 'Polling limit reached; resume using the same project and object IDs',
                    details={'object_ref': result.get('object_ref'), 'status': result.get('status')})
            time.sleep(min(args.interval, remaining))
    raise CLIError('invalid_input', 'Unsupported command')


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None,
         transport: httpx.BaseTransport | None = None) -> int:
    env = os.environ if environ is None else environ
    out, err, source = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    token = env.get('MVGP_TOKEN', '')
    def emit(value: Any, stream: TextIO) -> None:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        for credential in sorted({token, env.get('MVGP_ACCESS_CLIENT_ID', ''), env.get('MVGP_ACCESS_CLIENT_SECRET', ''),
                                  env.get('MVGP_ACCESS_TOKEN', '')}, key=len, reverse=True):
            if credential:
                text = text.replace(credential, '[credential omitted]')
        stream.write(text + '\n')
    try:
        args = parser().parse_args(argv)
        base, token = configuration(env)
        with Client(base, token, transport=transport, access=access_configuration(env, base)) as client:
            result = execute(client, args, source)
        emit(result, out)
        return 0
    except CLIError as exc:
        emit({'error': {'code': exc.code, 'message': exc.message, **exc.details}}, err)
        return EXIT.get(exc.code, 6)
    except (KeyError, TypeError, AttributeError, RecursionError):
        emit({'error': {'code': 'invalid_response', 'message': 'Platform response is missing required fields'}}, err)
        return 9
    except KeyboardInterrupt:
        emit({'error': {'code': 'interrupted', 'message': 'Stopped locally; queued platform work was not cancelled'}}, err)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
