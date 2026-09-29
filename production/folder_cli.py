"""The agent's folder commands: work in an HF-shaped project folder (production/FOLDER.md) and
sync it with the platform in a handful of calls.

  mvgp open  <dir>                      create or resume the project from brief.md, fetch the manuals, make the skeleton
  mvgp push  <dir>                      send the units that changed since the last push (image/quote/shoot push first)
  mvgp image <dir> <@tag…>              make each asset's image and put it in ASSETS/<KIND>/<tag>.png
  mvgp quote <dir> [<shot|scene>…]      what shooting those shots would cost
  mvgp shoot <dir> <shot|scene>… [--review <file>]   order four takes of each; the reviewer's notes travel with it
  mvgp pull  <dir> [--all]              write log.md and download the picks (every take with --all)

The folder is the writer's working copy; the platform keeps the records. What was last sent is kept in
`<dir>/.mvgp/state.json` (unit key → platform object and the digest sent), so an unchanged unit is never sent
again, and every request's idempotency key is derived from the unit, its base revision and its digest.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from production import folder as folders

SKELETON_DIRS = ('ASSETS/CHARACTERS', 'ASSETS/LOCATIONS', 'ASSETS/PROPS')


class Requests(Protocol):
    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None, content: Iterable[bytes] | None = None,
                headers: dict[str, str] | None = None) -> Any: ...

    def download(self, path: str, dest: Path, *, params: dict[str, Any] | None = None, sha256: str) -> None: ...


class FolderCommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def key(*parts: Any) -> str:
    """A request's idempotency key from what it sends, so a repeated command repeats nothing."""
    return 'f-' + hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:40]


def _state(root: Path) -> dict[str, Any]:
    path = root / '.mvgp' / 'state.json'
    return json.loads(path.read_text()) if path.exists() else {'units': {}}


def _save_state(root: Path, state: dict[str, Any]) -> None:
    folder = root / '.mvgp'
    folder.mkdir(mode=0o700, exist_ok=True)
    (folder / 'state.json.tmp').write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True))
    (folder / 'state.json.tmp').replace(folder / 'state.json')


def _brief(root: Path) -> tuple[str | None, str]:
    brief = root / 'brief.md'
    if not brief.exists():
        raise FolderCommandError('invalid_input', f'{brief} is missing: write the brief first (production/FOLDER.md)')
    text = brief.read_text()
    first, _, rest = text.partition('\n')
    match = folders.PROJECT_LINE.match(first)
    return (match[1], rest) if match else (None, text)


def open_project(client: Requests, root: Path, *, branch: str = 'original') -> dict[str, Any]:
    """create the project from brief.md (or resume the one it names), write the id back as
    brief.md's first line, fetch the manuals beside the films (`<films>/.manuals/<version>/`), make the skeleton."""
    root = Path(root).absolute()
    if not root.is_dir():
        raise FolderCommandError('invalid_input', f'{root} is not a folder')
    pid, text = _brief(root)
    created = False
    if pid is None:
        made = client.request('POST', '/v1/projects', body={
            'idempotency_key': key('open', str(root), hashlib.sha256(text.encode()).hexdigest()), 'expected_revision': 0,
            'title': root.name, 'branch': branch, 'brief': text.strip()})
        pid, created = made['project_id'], True
        (root / 'brief.md').write_text(f'project: {pid}\n' + text)
    else:
        client.request('GET', f'/v1/projects/{pid}')  # the project exists and this agent may use it
    manuals = client.request('GET', f'/v1/projects/{pid}/playbook')
    folder = root.parent / '.manuals' / str(manuals['version'])
    folder.mkdir(parents=True, exist_ok=True)
    for item in manuals['files']:
        name, body = str(item['name']), str(item['text'])
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,79}\.md', name) or hashlib.sha256(body.encode()).hexdigest() != item['sha256']:
            raise FolderCommandError('invalid_response', 'The platform returned manuals that do not match their own hashes')
        (folder / name).write_text(body)
    made_now = []
    for name in SKELETON_DIRS:
        if not (root / name).exists():
            (root / name).mkdir(parents=True)
            made_now.append(name + '/')
    for name, starter in (('script.md', ''), ('registry.md', '## Looks\n')):
        if not (root / name).exists():
            (root / name).write_text(starter)
            made_now.append(name)
    state = _state(root)
    if created or state.get('project_id') not in (None, pid):
        state = {'units': {}}  # a copied folder: nothing of it is on this project yet (bug hunt 2026-09-27)
    state.update(project_id=pid, manuals=manuals['version'])
    _save_state(root, state)
    return {'project_id': pid, 'created': created, 'manuals': {'version': manuals['version'], 'folder': str(folder)},
            'skeleton_made': made_now}


RECIPES = {'character': 'base-portrait', 'location': 'location-angle', 'prop': 'diagram'}
ROLES = {'character': ('visual', 'character'), 'location': ('world', 'environment'), 'prop': ('visual', 'prop')}
ORDER = ('brief', 'script', 'look', 'asset', 'scene', 'shot')


def _project(root: Path) -> str:
    pid, _ = _brief(root)
    if pid is None:
        raise FolderCommandError('invalid_input', 'Run `mvgp open <dir>` first: brief.md names no project')
    kept = _state(root).get('project_id')
    if kept is not None and kept != pid:
        raise FolderCommandError('invalid_input', f'.mvgp/state.json belongs to {kept}, but brief.md names {pid}: '
                                 'run `mvgp open <dir>` for this project, or put back the brief line')
    return pid


def _changed_fields(before: Any, after: Any) -> list[str]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ['text'] if before != after else []
    return sorted(k for k in {*before, *after} if before.get(k) != after.get(k))


def _upload(client: Requests, pid: str, path: Path, logical: str) -> dict[str, Any]:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    media_type = folders.MEDIA_SUFFIXES.get(path.suffix.lower(), 'image/png')  # a hand-placed video too
    meta = {'idempotency_key': key('upload', digest, logical), 'logical_path': logical, 'media_type': media_type,
            'byte_length': len(data), 'sha256': digest}
    made = client.request('POST', f'/v1/projects/{pid}/uploads', content=[data],
                          headers={'X-MVGP-Upload': json.dumps(meta), 'Content-Type': 'application/octet-stream'})
    return made['object_ref']


def _records(units: list[folders.Unit], state: dict[str, Any], manuals: str | None) -> list[dict[str, Any]]:
    """What each platform record should hold, in dependency order (scenes before their shots)."""
    by_key = {u.key: u for u in units}
    records = []
    for unit in sorted(units, key=lambda u: (ORDER.index(u.kind) if u.kind in ORDER else 99, u.key)):
        c = unit.content
        if unit.kind in ('brief', 'script'):
            records.append({'key': unit.key, 'kind': unit.kind, 'path': unit.path, 'content': c['text'] or '(empty)', 'digest': unit.digest})
        elif unit.kind == 'look':
            records.append({'key': unit.key, 'kind': 'asset', 'path': f"registry/looks/{c['name']}.json", 'digest': unit.digest,
                            'content': {'type': 'asset', 'role': 'look', 'tag': f"@look_{c['name']}", 'definition': c['text']}})
        elif unit.kind == 'asset':
            role, category = ROLES[c['kind']]
            image = by_key.get(f"image:{c['tag']}")
            definition: Any = c['descriptor']
            if image is not None:
                definition = {'recipe': RECIPES[c['kind']], 'description': image.content['prompt'], 'visual_treatment': 'photoreal',
                              'descriptor': c['descriptor'], **({'playbook_version': manuals} if manuals else {})}
            records.append({'key': unit.key, 'kind': 'asset', 'path': f"registry/{c['tag'][1:]}.json",
                            'digest': hashlib.sha256((unit.digest + (image.digest if image else '')).encode()).hexdigest(),
                            'content': {'type': 'asset', 'role': role, 'category': category, 'tag': c['tag'], 'definition': definition},
                            'image': (unit.path, image) if image else None})
        elif unit.kind == 'scene':
            text = f"# SCENE {c['number']} - {c['name']}\n\n{c['text']}\n\n## 镜头清单\n" + '\n'.join(c['shots'])
            records.append({'key': unit.key, 'kind': 'scene', 'path': unit.path, 'content': text, 'digest': unit.digest})
        elif unit.kind == 'shot':
            production: dict[str, Any] = {'prompt': c['prompt']}
            if c['look']:
                production['look'] = f"look_{c['look']}"
            if c.get('resolution'):
                production['resolution'] = c['resolution']
            records.append({'key': unit.key, 'kind': 'shot', 'path': f"{Path(unit.path).parent.as_posix()}/{c['number']}.json",
                            'digest': unit.digest, 'scene': f"scene:{c['scene']}", 'source': c.get('source'),
                            'content': {'shot': c['label'], 'The material': {'the running time in seconds': c['seconds'],
                                                                             'the action in one to three sentences': c['goal']},
                                        'Direction': {'the goal of the shot in one line': c['goal']}, '_production': production}})
    return records


def push(client: Requests, root: Path) -> dict[str, Any]:
    """send every unit that changed since the last push, and nothing else. A changed record
    is revised at its current platform revision (the service may have moved it, e.g. by binding an image), keeping
    the image the platform holds unless the folder has a new one; a shot's version note names what changed."""
    root = Path(root).absolute()
    pid = _project(root)
    units = folders.read(root)
    state = _state(root)
    records = _records(units, state, state.get('manuals'))
    sent, unchanged = [], 0
    refs = {k: v['ref'] for k, v in state['units'].items()}
    for record in records:
        known = state['units'].get(record['key'])
        image_sha = None
        if record.get('image'):
            _, image = record['image']
            image_sha = image.content['image_sha256']
        if known and known['digest'] == record['digest'] and known.get('image_sha256') == image_sha:
            unchanged += 1
            continue
        content = record['content']
        dependencies: list[dict[str, Any]] = []
        if record['kind'] == 'shot':
            scene = refs.get(record['scene'])
            if scene is None:
                raise FolderCommandError('invalid_input', f"{record['key']}: its scene was not pushed")
            dependencies.append(scene)
            if record['source']:
                # A recreation shot lists the source-understanding it recreates (AGENT_GUIDE; production/FOLDER.md).
                cited = client.request('GET', f"/v1/projects/{pid}/artifacts/{record['source']}")
                if cited.get('kind') != 'source-understanding':
                    raise FolderCommandError('invalid_input', f"{record['key']}: source {record['source']} is not a source-understanding")
                dependencies.append(cited['object_ref'])
            before = (known or {}).get('content') or {}
            if known:
                earlier = {k: v for k, v in (before.get('_production') or {}).items() if k != 'change_note'}
                fields = _changed_fields(earlier, content['_production']) + [
                    f for f in ('The material', 'Direction') if before.get(f) != content.get(f)]
                content = {**content, '_production': {**content['_production'],
                           'change_note': 'Changed in the folder: ' + ', '.join(fields or ['nothing but the order'])}}
        if record['kind'] == 'asset' and isinstance(content, dict) and content.get('role') != 'look':
            current_refs: list[dict[str, Any]] = []
            if known:
                current = client.request('GET', f"/v1/projects/{pid}/artifacts/{known['ref']['object_id']}")
                current_refs = list((current['details'].get('content') or {}).get('media_refs') or [])
            if image_sha and image_sha != (known or {}).get('image_sha256'):
                _, image = record['image']
                suffix = image.content.get('media_file') or '.png'
                placed = root / Path(image.path).with_suffix(suffix)
                current_refs = [_upload(client, pid, placed, Path(image.path).with_suffix(suffix).as_posix())]
            content = {**content, 'media_refs': current_refs}
            dependencies.extend(current_refs)
        if known:
            current = client.request('GET', f"/v1/projects/{pid}/artifacts/{known['ref']['object_id']}")
            revision = current['object_ref']['revision']
            made = client.request('PUT', f"/v1/projects/{pid}/artifacts/{known['ref']['object_id']}", body={
                'idempotency_key': key('revise', record['key'], revision, record['digest'], image_sha), 'expected_revision': revision,
                'kind': record['kind'], 'logical_path': record['path'], 'content': content, 'dependencies': dependencies})
        else:
            made = client.request('POST', f'/v1/projects/{pid}/artifacts', body={
                'idempotency_key': key('draft', record['key'], record['digest'], image_sha), 'expected_revision': 0,
                'kind': record['kind'], 'logical_path': record['path'], 'content': content, 'dependencies': dependencies})
        refs[record['key']] = made['object_ref']
        state['units'][record['key']] = {**(known or {}), 'ref': made['object_ref'], 'digest': record['digest'], 'image_sha256': image_sha,
                                         'content': content if record['kind'] == 'shot' else None}
        _save_state(root, state)
        sent.append(record['key'])
    return {'project_id': pid, 'sent': sent, 'unchanged': unchanged}


FOLDERS = {kind: name for name, kind in folders.KINDS.items()}
TERMINAL = {'succeeded', 'completed', 'failed', 'cancelled', 'unknown'}


def _land(client: Requests, root: Path, pid: str, tag: str, kind: str, media: dict[str, Any]) -> str:
    """Put the finished image at ASSETS/<KIND>/<tag>.png; a different picture already there moves to .mvgp/replaced/."""
    read = client.request('GET', f"/v1/projects/{pid}/artifacts/{media['object_id']}", params={'revision': media['revision']})
    sha = str(read['details']['sha256'])
    target = root / 'ASSETS' / FOLDERS[kind] / f'{tag}.png'
    incoming = target.with_name(f'.{tag}.incoming')
    client.download(f"/v1/projects/{pid}/media/{media['object_id']}", incoming, params={'revision': media['revision']}, sha256=sha)
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        old = hashlib.sha256(target.read_bytes()).hexdigest()
        replaced = root / '.mvgp' / 'replaced'
        replaced.mkdir(parents=True, exist_ok=True)
        target.replace(replaced / f'{tag}-{old[:12]}.png')
    incoming.replace(target)
    return sha


def image(client: Requests, root: Path, tags: list[str], *, wait_seconds: float = 300, interval: float = 3,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """push, then make each asset's image from ASSETS/<KIND>/<tag>.md and put it beside it.
    An order still out for the same image prompt is waited for, never placed again (it may have finished and been
    bound to the asset meanwhile); once it has landed or failed, running the command again makes a new image."""
    root = Path(root).absolute()
    pid = _project(root)
    pushed = push(client, root)
    state = _state(root)
    units = {u.key: u for u in folders.read(root)}
    problems = [f'{tag} is not an asset in registry.md' for tag in tags if f'asset:{tag}' not in units]
    problems += [f'{tag}: write its image prompt in ASSETS/<KIND>/{tag}.md first' for tag in tags
                 if f'asset:{tag}' in units and f'image:{tag}' not in units]
    if problems:
        raise FolderCommandError('invalid_input', '; '.join(problems))
    jobs: dict[str, dict[str, Any]] = {}
    for tag in dict.fromkeys(tags):
        entry = state['units'][f'asset:{tag}']
        prompt = units[f'image:{tag}'].digest
        pending = entry.get('pending_image')
        if not pending or pending['prompt'] != prompt:
            # Recorded before anything is sent, so a lost answer is replayed with the same keys and bodies, never
            # sent anew (bug hunt 2026-09-27: a lost submit answer plus a rerun paid twice once the image bound).
            asset = client.request('GET', f"/v1/projects/{pid}/artifacts/{entry['ref']['object_id']}")['object_ref']
            method_ref = entry.get('method')
            pending = {'prompt': prompt, 'attempt': entry.get('image_attempt', 0), 'asset': asset,
                       'method_expected': method_ref['revision'] if method_ref else asset['revision']}
            entry['pending_image'] = pending
            _save_state(root, state)
        if 'job' not in pending:
            asset, n = pending['asset'], pending['attempt']
            if 'method' not in pending:
                pending['method'] = entry['method'] = client.request('POST', f'/v1/projects/{pid}/method-selections', body={
                    'idempotency_key': key('method', tag, asset['revision'], prompt, n), 'expected_revision': pending['method_expected'],
                    'target': asset, 'method_id': 'mvgp-image-generate-v1',
                    'rationale': f'Make the image of {tag} from its image prompt.'})['object_ref']
                _save_state(root, state)
            if 'candidate' not in pending:
                pending['candidate'] = client.request('POST', f'/v1/projects/{pid}/candidates', body={
                    'idempotency_key': key('prepare', tag, asset['revision'], prompt, n), 'expected_revision': asset['revision'],
                    'target': asset, 'task': 'image', 'method_selection': pending['method'], 'inputs': []})['object_ref']
                _save_state(root, state)
            pending['job'] = client.request('POST', f'/v1/projects/{pid}/submissions', body={
                'idempotency_key': key('submit', tag, asset['revision'], prompt, n),
                'expected_revision': pending['candidate']['revision'], 'candidate_id': pending['candidate']['object_id']})['object_ref']
            _save_state(root, state)
        jobs[tag] = pending['job']
    deadline = clock() + max(0.0, min(wait_seconds, 600))
    done: dict[str, dict[str, Any]] = {}
    while True:
        for tag, job in jobs.items():
            if tag not in done:
                read = client.request('GET', f"/v1/projects/{pid}/artifacts/{job['object_id']}")
                if read.get('status') in TERMINAL:
                    done[tag] = read
        if len(done) == len(jobs) or clock() >= deadline:
            break
        sleep(max(0.05, min(interval, deadline - clock())))
    images = []
    for tag, job in jobs.items():
        kind = units[f'asset:{tag}'].content['kind']
        read = done.get(tag)
        if read is None:
            images.append({'tag': tag, 'state': 'waiting', 'job': job['object_id'],
                           'next': 'Run the same image command again to keep waiting; nothing is sent twice.'})
        elif read['status'] in ('succeeded', 'completed') and (read.get('details') or {}).get('result'):
            sha = _land(client, root, pid, tag, kind, read['details']['result'])
            images.append({'tag': tag, 'state': 'succeeded', 'path': f'ASSETS/{FOLDERS[kind]}/{tag}.png', 'sha256': sha})
        else:
            images.append({'tag': tag, 'state': read['status'], 'job': job['object_id'],
                           'error': (read.get('details') or {}).get('last_error')})
    for item in images:
        entry = state['units'][f"asset:{item['tag']}"]
        if item['state'] == 'succeeded':
            entry.pop('pending_image', None)
        elif item['state'] in ('failed', 'cancelled'):
            # Nothing came of it: the next run orders a new image (new keys), even with the same prompt.
            entry['image_attempt'] = entry.pop('pending_image', {}).get('attempt', 0) + 1
        # 'waiting' and 'unknown' keep the order: it may still finish and bind; the next run waits for it.
    # The platform already holds each landed image (bound to its asset), so the next push must not send it back.
    landed = {i['tag'] for i in images if i['state'] == 'succeeded'}
    for record in _records(folders.read(root), state, state.get('manuals')):
        if record['key'].startswith('asset:') and record['content']['tag'] in landed:
            entry = state['units'][record['key']]
            entry['ref'] = client.request('GET', f"/v1/projects/{pid}/artifacts/{entry['ref']['object_id']}")['object_ref']
            entry.update(digest=record['digest'], image_sha256=record['image'][1].content['image_sha256'])
    _save_state(root, state)
    return {'project_id': pid, 'pushed': pushed['sent'], 'images': images}


def _cards(root: Path, state: dict[str, Any], names: list[str]) -> list[str]:
    """Unit keys of the shots named: a label (S01-010), scene:shot (01:010) or a scene number (01, every shot in it);
    no names → every shot of the folder, in folder order."""
    order = {u.key: n for n, u in enumerate(folders.read(root))}
    # Only shots still in the folder (a deleted or renumbered shot is never quoted or shot; bug hunt 2026-09-27).
    shots = sorted((k for k in state['units'] if k.startswith('shot:') and k in order), key=order.__getitem__)
    if not names:
        return shots
    chosen: list[str] = []
    unknown: list[str] = []
    for name in names:
        match = re.fullmatch(r'(?:S)?(\d{1,3})(?:[-:](\S+))?', name.strip())
        if match is None:
            unknown.append(name)
            continue
        scene = f'{int(match[1]):02d}'
        found = [k for k in shots if k == f'shot:{scene}:{match[2]}'] if match[2] else [k for k in shots if k.startswith(f'shot:{scene}:')]
        if not found:
            unknown.append(name)
        chosen.extend(k for k in found if k not in chosen)
    if unknown:
        raise FolderCommandError('invalid_input', 'No such shot or scene in the folder: ' + ', '.join(unknown))
    return chosen


def _current(client: Requests, pid: str, state: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    refs = []
    for unit in keys:
        ref = client.request('GET', f"/v1/projects/{pid}/artifacts/{state['units'][unit]['ref']['object_id']}")['object_ref']
        state['units'][unit]['ref'] = ref
        refs.append(ref)
    return refs


def quote(client: Requests, root: Path, names: list[str], *, takes: int = 4) -> dict[str, Any]:
    """push, then what shooting those shots (default: all) would cost; nothing is spent."""
    root = Path(root).absolute()
    pid = _project(root)
    pushed = push(client, root)
    state = _state(root)
    keys = _cards(root, state, names)
    refs = _current(client, pid, state, keys)
    priced = client.request('GET', f'/v1/projects/{pid}/quote',
                            params={'card': [r['object_id'] for r in refs], 'takes': takes})
    return {'project_id': pid, 'pushed': pushed['sent'], **priced}


def _review(path: Path | None) -> dict[str, Any] | None:
    """The fresh reviewer's notes and the writer's answers (ShootReview): {reviewer, playbook_version?, notes: [{line,
    note, answer, shot?}]}. The platform shows them on the desk and never blocks on them; without one the desk says 没审."""
    if path is None:
        return None
    try:
        review = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise FolderCommandError('invalid_input', f'{path}: the review file is not readable JSON') from None
    if not isinstance(review, dict):
        raise FolderCommandError('invalid_input', f'{path}: the review file is one JSON object {{reviewer, notes}}')
    return review


def shoot(client: Requests, root: Path, names: list[str], *, takes: int = 4, review: Path | None = None) -> dict[str, Any]:
    """push, then one shoot order for those shots (four takes each by default) with the
    reviewer's notes. The platform prepares, fires, waits and puts the takes on the owner's desk. The order's key
    comes from the exact card versions, so running it again sends nothing new; a changed card is a new order."""
    root = Path(root).absolute()
    pid = _project(root)
    if not names:
        raise FolderCommandError('invalid_input', 'Name the shots or scenes to shoot (quote first: `mvgp quote <dir>`)')
    notes = _review(review)
    pushed = push(client, root)
    state = _state(root)
    keys = _cards(root, state, names)
    refs = _current(client, pid, state, keys)
    # The assets and looks the prompts use can change without the cards changing (a new picture or descriptor):
    # their versions are part of the order, so such a change is a new order, not a replay of the old one.
    constants = sorted((k, v['ref']['object_id'], v['ref']['revision']) for k, v in state['units'].items()
                       if k.startswith(('asset:', 'look:')))
    body: dict[str, Any] = {'idempotency_key': key('shoot', [(r['object_id'], r['revision']) for r in refs], takes, notes,
                                                   constants),
                            'cards': refs, 'takes': takes}
    if notes is not None:
        body['review'] = notes
    order = client.request('POST', f'/v1/projects/{pid}/shoot-orders', body=body)
    _save_state(root, state)
    return {'project_id': pid, 'pushed': pushed['sent'], 'shots': [k.split(':', 1)[1] for k in keys], 'order': order}


def _file_name(label: str, name: str) -> str:
    """`S01-010 第1版 第2条 正片.mp4` from the shot label and the take's name on the project page."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', f"{label} {name.replace(' · ', ' ').replace('第 ', '第').replace(' 条', '条').replace(' 版', '版')}")


def _cell(value: Any) -> str:
    return ' '.join(str(value).split()).replace('|', '/') if value not in (None, '') else '—'


def pull(client: Requests, root: Path, *, every: bool = False) -> dict[str, Any]:
    """write log.md from the platform (per shot: version, what changed, takes, verdict, the
    owner's reason; the reviewer; the owner's notes by take and second) and download the picks (every take with
    --all) into TAKES/. It only reads the platform; a file already downloaded is not fetched again."""
    root = Path(root).absolute()
    pid = _project(root)
    state = _state(root)
    tree = client.request('GET', f'/v1/projects/{pid}/project-tree')
    feed = client.request('GET', f'/v1/projects/{pid}/review-feed')
    order = {u.key: n for n, u in enumerate(folders.read(root))}
    shots = sorted((k for k in state['units'] if k.startswith('shot:') and k in order), key=order.__getitem__)
    by_folder = {f['id']: f for f in tree.get('folders', [])}
    items: dict[str, list[dict[str, Any]]] = {}
    for item in tree.get('items', []):
        items.setdefault(item['folder'], []).append(item)
    notes = [(n['object_ref']['object_id'], n.get('details') or {}) for n in feed.get('notes', [])]
    pulled = state.setdefault('pulled', {})
    lines = ['# log.md', '', 'Written by `mvgp pull` from the platform; never edited by hand (production/FOLDER.md).', '']
    downloaded, kept = [], []
    used: dict[str, str] = {}
    for unit in shots:
        oid = state['units'][unit]['ref']['object_id']
        folder = by_folder.get(f'shot:{oid}')
        label = f"S{unit.split(':')[1]}-{unit.split(':')[2]}"
        if folder is None:
            lines += [f'## {label} · 未上平台', '']
            continue
        facts = folder.get('shot') or {}
        lines += [f"## {label} · {_cell(facts.get('goal'))} · {facts.get('status', '未拍')}", '',
                  '| version | what changed | takes | verdict | owner\'s reason |', '|---|---|---|---|---|']
        lines += [f"| {e['version']} | {_cell(e.get('change_note'))} | {e.get('takes', 0)} | {_cell(e.get('verdict'))} | "
                  f"{_cell(e.get('reason'))} |" for e in folder.get('log') or []]
        review = folder.get('review') or {}
        lines += ['', f"Reviewer: {review.get('reviewer') or '没审'}" + ('' if review.get('manuals_current', True) is not False
                                                                       else ' · 手册不是最新')]
        lines += [f"- {n.get('line')}: {n.get('note')} → {n.get('answer')}" for n in review.get('notes') or []]
        takes = [i for i in items.get(f'shot:{oid}', []) if i.get('kind') == 'video' and not (i.get('details') or {}).get('source')]
        names = {i['media']['object_id']: i['name'] for i in takes if i.get('media')}
        mine = {oid, *names}
        shot_notes = [d for _, d in notes if any(isinstance(r, dict) and r.get('object_id') in mine
                                                 for r in (d.get('target'), d.get('take')))]
        if shot_notes:
            lines.append('Owner notes:')
            for n in sorted(shot_notes, key=lambda n: (names.get((n.get('take') or {}).get('object_id'), ''), n.get('at_seconds') or 0)):
                where = names.get((n.get('take') or {}).get('object_id'), '整条镜头')
                at = f" at {n['at_seconds']:g} s" if isinstance(n.get('at_seconds'), (int, float)) else ''
                lines.append(f"- {where}{at}: {_cell(n.get('text'))}")
        picked = {i['media']['object_id'] for i in takes if (i.get('details') or {}).get('picked')}
        wanted = [i for i in takes if every or (i.get('details') or {}).get('picked')
                  or (i.get('details') or {}).get('completes') in picked]
        files = []
        for item in wanted:
            media = item['media']
            name = _file_name(label, item['name']) + '.mp4'
            if name in used and used[name] != media['object_id']:
                # A reshoot of the same version numbers its takes 第1–4条 again: keep both (bug hunt 2026-09-27).
                name = f"{name[:-4]} {media['object_id'][-6:]}.mp4"
            used[name] = media['object_id']
            path = root / 'TAKES' / name
            seen = f"{media['object_id']}:{media['revision']}"
            if pulled.get(seen) == name and path.exists():
                kept.append(f'TAKES/{name}')
            else:
                path.parent.mkdir(exist_ok=True)
                sha = client.request('GET', f"/v1/projects/{pid}/artifacts/{media['object_id']}",
                                     params={'revision': media['revision']})['details']['sha256']
                client.download(f"/v1/projects/{pid}/media/{media['object_id']}", path,
                                params={'revision': media['revision']}, sha256=sha)
                pulled[seen] = name
                downloaded.append(f'TAKES/{name}')
            files.append(f'TAKES/{name}' + (' (picked)' if media['object_id'] in picked else ''))
        if files:
            lines += ['Files:'] + [f'- {f}' for f in files]
        lines.append('')
    (root / 'log.md').write_text('\n'.join(lines))
    _save_state(root, state)
    return {'project_id': pid, 'log': 'log.md', 'downloaded': downloaded, 'already_here': kept}
