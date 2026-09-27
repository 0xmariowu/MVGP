"""Read an HF-shaped project folder into units (layout in production/FOLDER.md).

A unit is one thing the platform keeps a record of: the brief, the script, a look per world, an asset per tag
(its registry descriptor), an asset's image prompt (and image), a scene, a shot. Each has an identity key that
survives edits (kind + tag, scene number, scene + shot number) and a digest of its content, so a push sends only
what changed. The writer's text is kept exactly: a shot's prompt is everything under its header, only the blank
lines around it trimmed. A folder that cannot be read unambiguously raises FolderError listing every problem.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The files an asset may carry beside its prompt, with their media types (videos for Higgsfield).
MEDIA_SUFFIXES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
                  '.mp4': 'video/mp4', '.mov': 'video/quicktime'}
KINDS = {'CHARACTERS': 'character', 'LOCATIONS': 'location', 'PROPS': 'prop'}
# The same shape the prompt reads (production/prompt.py TAG): words joined by single hyphens, e.g. @kel, @kel_wet, @loc-2.
TAG = r'@[A-Za-z][A-Za-z0-9_]{0,63}(?:-[A-Za-z0-9_]{1,63}){0,4}'
ASSET_HEADER = re.compile(rf'^## ({TAG}) · (character|location|prop)\s*$')
LOOKS_HEADER = re.compile(r'^## Looks\s*$')
LOOK_HEADER = re.compile(r'^### ([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s*$')
SCENE_DIR = re.compile(r'^SCENE (\d{1,3}) - (.+)$')
SHOT_HEADER = re.compile(r'^## ([A-Za-z0-9][A-Za-z0-9._-]{0,15}) · (\d{1,2})\s*s · (.+?)\s*$')
LOOK_LINE = re.compile(r'^look:\s*([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s*$')
# A line meant as a shot header that does not parse: our separator, or "## <number> <seconds>s …" without it. Any other
# `## ` line is the writer's own heading and stays in the prompt (3 of the first 11 HF projects write them; re-audit 2026-09-27).
HEADER_ATTEMPT = re.compile(r'^## (?:.*·|\S{1,16}\s+\d{1,2}\s*s\b)')
SOURCE_LINE = re.compile(r'^source:\s*([A-Za-z0-9][A-Za-z0-9_-]{0,127})\s*$')
PROJECT_LINE = re.compile(r'^project:\s*(project_[A-Za-z0-9_-]+)\s*$')


class FolderError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__('; '.join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Unit:
    key: str                  # identity across edits: 'brief', 'script', 'look:city', 'asset:@kel', 'image:@kel', 'scene:01', 'shot:01:010A'
    kind: str                 # brief · script · look · asset · image · scene · shot
    path: str                 # the file it came from, relative to the folder
    content: dict[str, Any]
    digest: str               # sha256 of the content: a push sends a unit only when this changed


def _unit(key: str, kind: str, path: str, content: dict[str, Any]) -> Unit:
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return Unit(key, kind, path, content, digest)


def _trim(lines: list[str]) -> str:
    """The writer's text as written, only the blank lines around it removed."""
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return '\n'.join(lines)


def project_id(folder: Path) -> str | None:
    """The platform project this folder belongs to: `project: <id>` on brief.md's first line (written by `open`)."""
    brief = Path(folder) / 'brief.md'
    if not brief.exists():
        return None
    first = brief.read_text().split('\n', 1)[0]
    match = PROJECT_LINE.match(first)
    return match[1] if match else None


def _registry(text: str, path: str, problems: list[str]) -> list[Unit]:
    units: list[Unit] = []
    section: tuple[str, str] | None = None     # ('look', name) or ('asset', tag)
    kinds: dict[str, str] = {}
    body: list[str] = []
    in_looks = False
    def close() -> None:
        if section is None:
            return
        text = _trim(body)
        if not text:
            problems.append(f'{path}: {section[1]} has no text')
        elif section[0] == 'look':
            units.append(_unit(f'look:{section[1]}', 'look', path, {'name': section[1], 'text': text}))
        else:
            units.append(_unit(f'asset:{section[1]}', 'asset', path, {'tag': section[1], 'kind': kinds[section[1]], 'descriptor': text}))
    for n, line in enumerate(text.split('\n'), 1):
        if LOOKS_HEADER.match(line):
            close()
            section, body, in_looks = None, [], True
        elif (look := LOOK_HEADER.match(line)) and in_looks:
            close()
            section, body = ('look', look[1]), []
        elif asset := ASSET_HEADER.match(line):
            close()
            in_looks = False
            if asset[1] in kinds:
                problems.append(f'{path}:{n}: {asset[1]} is listed twice')
            kinds[asset[1]] = asset[2]
            section, body = ('asset', asset[1]), []
        elif line.startswith('## ') or (line.startswith('### ') and not in_looks):
            problems.append(f'{path}:{n}: not a registry header (## Looks, ### <world>, ## @tag · character|location|prop): {line[:60]}')
            close()
            section, body = None, []
        elif section is not None:
            body.append(line)
    close()
    return units


def _shotlist(text: str, path: str, scene: str, problems: list[str]) -> tuple[str, list[Unit]]:
    """The scene's own text (before the first shot) and its shots."""
    preamble: list[str] = []
    shots: list[Unit] = []
    header: re.Match[str] | None = None
    body: list[str] = []
    def close() -> None:
        if header is None:
            return
        lines, look, source = list(body), None, None
        while lines and not lines[0].strip():
            lines = lines[1:]
        # `look:` and `source:` (a recreation shot's source-understanding record), in either order, before the prompt.
        while lines:
            if named := LOOK_LINE.match(lines[0]):
                look = named[1]
            elif cited := SOURCE_LINE.match(lines[0]):
                source = cited[1]
            else:
                break
            lines = lines[1:]
        prompt = _trim(lines)
        number, seconds, goal = header[1], int(header[2]), header[3]
        if not prompt:
            problems.append(f'{path}: shot {number} has no prompt')
        if not 4 <= seconds <= 30:
            problems.append(f'{path}: shot {number} runs {seconds} s; fal takes 4–30 whole seconds')
        shots.append(_unit(f'shot:{scene}:{number}', 'shot', path, {
            'scene': scene, 'number': number, 'label': f'S{scene}-{number}', 'seconds': seconds, 'goal': goal,
            'look': look, 'prompt': prompt, **({'source': source} if source else {})}))
    for n, line in enumerate(text.split('\n'), 1):
        if SHOT_HEADER.match(line) or HEADER_ATTEMPT.match(line):
            close()
            header, body = SHOT_HEADER.match(line), []
            if header is None:
                problems.append(f'{path}:{n}: a shot header is "## <number> · <seconds>s · <goal>": {line[:60]}')
        elif header is not None:
            body.append(line)
        else:
            preamble.append(line)
    close()
    return _trim(preamble), shots


def read(folder: Path) -> list[Unit]:
    """Every unit of the folder, in a stable order. Raises FolderError listing every problem found."""
    root = Path(folder)
    problems: list[str] = []
    units: list[Unit] = []
    brief = root / 'brief.md'
    if brief.exists():
        lines = brief.read_text().split('\n')
        if lines and PROJECT_LINE.match(lines[0]):
            lines = lines[1:]
        units.append(_unit('brief', 'brief', 'brief.md', {'text': _trim(lines)}))
    else:
        problems.append('brief.md is missing')
    script = root / 'script.md'
    if script.exists():
        units.append(_unit('script', 'script', 'script.md', {'text': _trim(script.read_text().split('\n'))}))
    registry = root / 'registry.md'
    tags: dict[str, str] = {}
    if registry.exists():
        for unit in _registry(registry.read_text(), 'registry.md', problems):
            units.append(unit)
            if unit.kind == 'asset':
                tags[unit.content['tag']] = unit.content['kind']
    for folder_name, kind in KINDS.items():
        for prompt_file in sorted((root / 'ASSETS' / folder_name).glob('*.md')):
            tag = prompt_file.stem
            relative = prompt_file.relative_to(root).as_posix()
            if not re.fullmatch(TAG, tag):
                problems.append(f'{relative}: the file name is the asset tag, like @kel.md')
                continue
            if tags.get(tag) != kind:
                problems.append(f'{relative}: {tag} is not a {kind} in registry.md')
            # One picture or, for the Higgsfield route, one video (a previs, a turnaround) beside it.
            placed = [prompt_file.with_suffix(suffix) for suffix in MEDIA_SUFFIXES if prompt_file.with_suffix(suffix).exists()]
            if len(placed) > 1:
                problems.append(f'{relative}: one picture or one video per asset, found ' + ', '.join(p.name for p in placed))
            image = placed[0] if placed else prompt_file.with_suffix('.png')
            units.append(_unit(f'image:{tag}', 'image', relative, {
                'tag': tag, 'kind': kind, 'prompt': _trim(prompt_file.read_text().split('\n')),
                'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest() if image.exists() else None,
                **({'media_file': image.suffix} if image.suffix != '.png' else {})}))
    labels: set[str] = set()
    numbers: dict[str, str] = {}
    for scene_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith('SCENE')):
        match = SCENE_DIR.match(scene_dir.name)
        if match is None:
            problems.append(f'{scene_dir.name}: a scene folder is named "SCENE NN - NAME"')
            continue
        scene = f'{int(match[1]):02d}'
        if scene in numbers:
            problems.append(f'{scene_dir.name}: scene {scene} is also {numbers[scene]}')
            continue
        numbers[scene] = scene_dir.name
        shotlist = scene_dir / 'shotlist.md'
        relative = shotlist.relative_to(root).as_posix()
        text, shots = _shotlist(shotlist.read_text(), relative, scene, problems) if shotlist.exists() else ('', [])
        units.append(_unit(f'scene:{scene}', 'scene', relative, {'number': scene, 'name': match[2], 'text': text,
                                                                 'shots': [s.content['label'] for s in shots]}))
        for shot in shots:
            if shot.content['label'] in labels:
                problems.append(f"{relative}: shot {shot.content['number']} appears twice")
            labels.add(shot.content['label'])
            units.append(shot)
    looks = {u.content['name'] for u in units if u.kind == 'look'}
    for shot in (u for u in units if u.kind == 'shot'):
        if shot.content['look'] is not None and shot.content['look'] not in looks:
            problems.append(f"{shot.path}: shot {shot.content['number']} names look {shot.content['look']}, not in registry.md")
    if problems:
        raise FolderError(problems)
    return units
