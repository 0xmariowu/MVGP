"""The writer's whole prompt, sent verbatim plus only the allowed additions.

Owner 2026-09-26: the writer writes every section; the platform pastes HF constants (descriptors, the world's look;
cully:133) and adds duration, "No music." and the image binding only when the writer left them out. Every addition
is a pure insertion at an offset of the writer's text, so the writer's bytes never change and `check` can prove
that what went out is exactly the writer's text plus additions from their allowed sets.

The five additions (the allowed insertion rules):
- `bind`: `@ImageK ` before the writer's `@tag`, before every mention (`binding: every`) or only the first
  (`binding: first`); images are numbered by first appearance in the writer's text. A tag whose asset is a video
  (a previs or a turnaround, Higgsfield route) is bound as `@VideoK`, videos numbered apart.
- `descriptor`: only when no line holding the tag has four other words (the writer described it, adapted or not);
  after the tag + optional parenthetical + separator on its bare reference line, else a `@tag — descriptor` line at
  the end of the writer's references block, else a new ACTIVE REFERENCES block at the top.
- `look`: the shot's named look, only when the writer wrote no STYLE / LOOK / GRADE / COLOUR section and no `Style:`
  line; a STYLE block before QUALITY / POSITIVE…, else at the end.
- `duration`: `{N}s.` at the end, only when the writer stated no duration at all.
- `no_music`: `No music.` at the end, only when the text never mentions music, score or song.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# An @tag: word characters joined by single hyphens (same pattern as compiler.TAG_RE), not inside an e-mail address.
TAG = re.compile(r'(?<![\w@])@([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)')
# A header label at the start of a line: "ACTIVE REFERENCES", "— REFERENCE DEFINITIONS —", "COLOUR / LIGHT:", "GRADE:".
HEADER = re.compile(r"^[ \t]*(?:#+[ \t]*|[—–]+[ \t]*|\*\*)?(?P<label>[A-Z][A-Z0-9 /&+'’.-]*[A-Z0-9])[ \t\r]*(?:[:：—–|]|\*\*|$)")
REFERENCES = re.compile(r'^(?:ACTIVE REFERENCES|REFERENCE DEFINITIONS|REFERENCES?|CHARACTERS?|SUBJECT LOCK)\b')
CJK_REFERENCES = re.compile(r'^[ \t]*(?:参考|角色|人物)[^:：\n]{0,8}[:：]')
# A look header starts with a look word (after at most one word: "VISUAL STYLE") or ends with one ("COLOUR GRADE");
# a slug like "EXT. GRADE SCHOOL — DAY" is not one (bug hunt 2026-09-27).
LOOK_WORD = re.compile(r"^(?:[A-Z][A-Z'’]* )?(?:STYLE|LOOK|GRADE|COLOU?R)\b|\b(?:STYLE|LOOK|GRADE|COLOU?R)$")
LOOK_LINE = re.compile(r'^[ \t]*(?:(?:style|look|grade|colou?r)[ \t]*[:：]|(?:画风|风格|调色|色调)[^:：\n]{0,8}[:：])', re.IGNORECASE)
CLOSING = re.compile(r'^(?:QUALITY|POSITIVE)\b')
DURATION = re.compile(r'\b\d+(?:\.\d+)?\s*(?:–|-|to)?\s*\d*\s*(?:s|sec|secs|second|seconds)\b|\d+\s*秒')
# "late-20s", "in his 40s", "40s ex-con" are ages, not durations (cully's own descriptors).
AGE_BEFORE = re.compile(r'(?:early|mid|late)[- ]?$|\b(?:his|her|their|the|in)\s+$', re.IGNORECASE)
AGE_AFTER = re.compile(r'\s*(?:ex-|man\b|woman\b|men\b|women\b|guy\b|-year)', re.IGNORECASE)
MUSIC = re.compile(r'\b(?:music|musical|score|scored|song|songs|soundtrack)\b|音乐|配乐', re.IGNORECASE)
WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?|[㐀-鿿豈-﫿]")
# Before the tag on a bare reference line: a bullet, a header label with its colon, a NAME ("CHARACTERS: JACK @jack").
LEAD = re.compile(r"^[ \t]*(?:[-*•][ \t]+|\d+[.)][ \t]+)?(?:[A-Z][A-Z0-9 /&+'’-]*[:：][ \t]*)?(?:[A-Z][A-Z'’-]+[ \t]+)?$")
TAIL = re.compile(r'(?P<paren>[ \t]*[(\[][^)\]\n]*[)\]])?(?P<sep>[ \t]*(?:[:：]|—|–|-{1,2})[ \t]*)?(?P<rest>[^\n]*)')
ORDER = {'descriptor': 0, 'look': 1, 'bind': 2, 'duration': 3, 'no_music': 4}
# Advice only (HF practice from HF_CANONICAL.md §5-6): lens stated in 9 of 12, timed beats in 6 of 12 (half).
LENS = re.compile(r'\d+(?:\.\d+)?\s*(?:mm\b|°|毫米)|\bFOV\b', re.IGNORECASE)
TIMED_BEATS = re.compile(r'(?<![\d.])0(?:\.0+)?\s*(?:s|sec|秒)?\s*(?:–|-|to)\s*\d')
# State words a registry descriptor and the writer's own line may disagree on, folded to one word per state.
STATES = {'wet': 'wet', 'soaked': 'wet', 'drenched': 'wet', 'dripping': 'wet', 'dry': 'dry',
          'bloody': 'bloody', 'bloodied': 'bloody', 'blood': 'bloody', 'clean': 'clean', 'dirty': 'dirty',
          'muddy': 'dirty', 'grimy': 'dirty', 'masked': 'masked', 'unmasked': 'unmasked', 'bandaged': 'bandaged',
          'wounded': 'wounded', 'injured': 'wounded', 'burned': 'burned', 'burnt': 'burned', 'torn': 'torn',
          'shirtless': 'shirtless', 'naked': 'naked', 'sweaty': 'sweaty'}


def tags(text: str) -> list[str]:
    """Every @tag the writer used, in order of first appearance."""
    return list(dict.fromkeys(m[1] for m in TAG.finditer(text)))


def constants(assets: Mapping[str, Mapping[str, Any]], looks: Mapping[str, str], seconds: float,
              style: str = 'at') -> dict[str, Any]:
    """What `check` accepts as pasted text: the registry's descriptors and looks, the card's duration and, on the
    Higgsfield route, the reference style (`hf`: HF's own `<<<image_N>>>` / `<<<video_N>>>`)."""
    found = {'descriptors': {tag: a['descriptor'].strip() for tag, a in assets.items()
                             if isinstance(a.get('descriptor'), str) and a['descriptor'].strip()},
             'looks': [text.strip() for text in looks.values() if text.strip()], 'seconds': seconds}
    if style == 'hf':
        found['reference_style'] = 'hf'  # absent on the fal route, so earlier frozen constants keep their shape
    return found


def _durations(W: str) -> list[re.Match[str]]:
    """Every duration the writer stated, without ages written like decades."""
    def age(m: re.Match[str]) -> bool:
        if re.fullmatch(r'\d{3}0s', m[0]):
            return True  # a decade, "the 1980s"
        return bool(re.fullmatch(r'[1-9]0s', m[0]) and (AGE_BEFORE.search(W[max(0, m.start() - 12):m.start()])
                                                          or AGE_AFTER.match(W, m.end())))
    return [m for m in DURATION.finditer(W) if not age(m)]


def _seconds(seconds: float) -> str:
    return str(int(seconds)) if float(seconds).is_integer() else str(seconds)


def _look_body(look: str) -> str:
    return look if _is_look_header(look.split('\n', 1)[0]) else f'STYLE: {look}'


def _is_look_header(line: str) -> bool:
    header = HEADER.match(line)
    return bool(header and LOOK_WORD.search(header['label']) or LOOK_LINE.match(line))


def _token(media: Any, n: Any, style: Any = None) -> str:
    """The reference number as the route reads it: fal documents `@Image1` / `@Video1`;
    Higgsfield prompts use HF's own `<<<image_1>>>` / `<<<video_1>>>` (passport-rush: 6,452 of 6,750 prompts with a
    video reference write `<<<video_N>>>`, 44 `@Video`; the CLI stores `@Image1` verbatim, checked 2026-09-27)."""
    kind = 'video' if media == 'video' else 'image'
    return f'<<<{kind}_{n}>>>' if style == 'hf' else f'@{_label(kind)}{n}'


def _label(media: Any) -> str:
    """The number's word: an image is @ImageK, a video @VideoK; an addition without `media` is an image."""
    return 'Video' if media == 'video' else 'Image'


def _renders(kind: str, addition: Mapping[str, Any], found: Mapping[str, Any]) -> set[str]:
    """Every text an addition of this kind may carry, given the registry constants."""
    if kind == 'bind':
        return {f"{_token(addition.get('media'), addition.get('n'), found.get('reference_style'))} "}
    if kind == 'look':
        return {body + '\n\n' for body in map(_look_body, found['looks'])} | {'\n\n' + body for body in map(_look_body, found['looks'])}
    if kind == 'duration':
        n = _seconds(found['seconds'])
        return {f'\n\n{n}s.', f' {n}s.', f'{n}s.'}
    if kind == 'no_music':
        return {'\n\nNo music.', ' No music.', 'No music.'}
    if kind == 'descriptor':
        descriptors = found['descriptors']
        if 'tags' in addition:
            listed = addition['tags']
            if not listed or any(tag not in descriptors for tag in listed):
                return set()
            numbered, media, style = addition.get('numbers') or {}, addition.get('media') or {}, found.get('reference_style')
            return {'ACTIVE REFERENCES:\n' + ''.join(f"{_token(media.get(tag), numbered[tag], style) + ' ' if tag in numbered else ''}@{tag} — "
                                                      f'{descriptors[tag]}\n' for tag in listed) + '\n'}
        d = descriptors.get(addition.get('tag'))
        if not d:
            return set()
        number = (addition.get('numbers') or {}).get(addition.get('tag'))
        if number is not None:
            token = _token((addition.get('media') or {}).get(addition.get('tag')), number, found.get('reference_style'))
            return {f"\n{token} @{addition['tag']} — {d}"}  # the added line is the tag's first mention
        return {d, d + ' ', ' ' + d, ' — ' + d, f"\n@{addition['tag']} — {d}"}
    return set()


def _lines(text: str) -> list[tuple[int, str]]:
    """(offset, line) for every line of the text, without the newline."""
    out, at = [], 0
    for line in text.split('\n'):
        out.append((at, line))
        at += len(line) + 1
    return out


def _described(text: str, tag: str) -> bool:
    """The writer described the tag somewhere: a line that holds it plus four other words (under any header)."""
    for _, line in _lines(text):
        if any(m[1] == tag for m in TAG.finditer(line)) and len(WORD.findall(TAG.sub(' ', line))) >= 4:
            return True
    return False


def _inline(text: str, tag: str, descriptor: str) -> tuple[int, str] | None:
    """Where the descriptor goes on the tag's bare reference line (`@kel (CAL):`, `- @room`), if it has one."""
    for start, line in _lines(text):
        for m in TAG.finditer(line):
            if m[1] != tag or not LEAD.match(line[:m.start()]):
                continue
            tail = TAIL.match(line, m.end())
            assert tail is not None
            if tail['sep'] is not None and tail['sep'].strip():
                at = start + tail.end('sep')
                if tail['rest'].strip():
                    return at, descriptor + ' '
                return at, descriptor if tail['sep'][-1:] in ' \t' else ' ' + descriptor
            if not tail['rest'].strip():
                return start + (tail.end('paren') if tail['paren'] else m.end()), ' — ' + descriptor
    return None


def _references_end(text: str) -> int | None:
    """End of the last entry line (the header or a line led by a tag) of the writer's first references block."""
    end = None
    for start, line in _lines(text):
        header = HEADER.match(line)
        if end is None:
            if header and REFERENCES.match(header['label']) or CJK_REFERENCES.match(line):
                end = start + len(line)
            continue
        if header or CJK_REFERENCES.match(line) or LOOK_LINE.match(line):
            break
        if re.match(r'^[ \t]*(?:[-*•][ \t]+|\d+[.)][ \t]+)?@', line):
            end = start + len(line)
    return end


def build(W: str, assets: Mapping[str, Mapping[str, Any]], looks: Mapping[str, str], look_name: str | None,
          seconds: float, binding: str, style: str = 'at') -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """The prompt that goes out, its images in order, and the additions that turn W into it.

    `assets` maps a tag to {'descriptor': str | None, 'image': <reference> | None, 'video': <reference> | None};
    `looks` maps a look name to its text (one per world); `look_name` is the look the shot names (default: the only
    one). Each numbered entry carries `media` ('image' or 'video'); images and videos are numbered apart.
    """
    if binding not in ('every', 'first'):
        raise ValueError(f'binding must be every or first, not {binding!r}')
    if look_name is not None and look_name not in looks:
        raise ValueError(f'the shot names look {look_name!r}, the registry has {sorted(looks)}')
    additions: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    numbers: dict[str, int] = {}
    media: dict[str, str] = {}
    for tag in tags(W):
        entry = assets.get(tag, {})
        kind = 'video' if entry.get('video') is not None else 'image' if entry.get('image') is not None else None
        if kind is not None:
            media[tag] = kind
            numbers[tag] = sum(1 for i in images if i['media'] == kind) + 1
            images.append({'n': numbers[tag], 'tag': tag, 'image': entry[kind], 'media': kind})
    bound: set[str] = set()
    for m in TAG.finditer(W):
        if m[1] in numbers and (binding == 'every' or m[1] not in bound):
            bound.add(m[1])
            addition = {'kind': 'bind', 'at': m.start(), 'text': f'{_token(media[m[1]], numbers[m[1]], style)} ', 'tag': m[1], 'n': numbers[m[1]]}
            if media[m[1]] == 'video':
                addition['media'] = 'video'
            additions.append(addition)
    found = constants(assets, looks, seconds)
    top: list[str] = []
    block_end = _references_end(W)
    for tag in tags(W):
        descriptor = found['descriptors'].get(tag)
        if descriptor is None or _described(W, tag):
            continue
        inline = _inline(W, tag, descriptor)
        if inline:
            at, text = inline
            if at > len(W.rstrip()):  # a bare `@kel:` with trailing spaces at the very end: never after the tail
                at, text = len(W.rstrip()), ' ' + descriptor
            additions.append({'kind': 'descriptor', 'at': at, 'text': text, 'tag': tag})
        elif block_end is not None:
            additions.append({'kind': 'descriptor', 'at': block_end, 'text': f'\n@{tag} — {descriptor}', 'tag': tag})
        else:
            top.append(tag)
    if top:
        text = 'ACTIVE REFERENCES:\n' + ''.join(f"@{tag} — {found['descriptors'][tag]}\n" for tag in top) + '\n'
        additions.append({'kind': 'descriptor', 'at': 0, 'text': text, 'tags': top})
    if binding == 'first':
        _bind_added_lines(W, additions, numbers, media, style)
    end = len(W.rstrip())
    tail: list[str] = []
    name = look_name if look_name is not None else (next(iter(looks)) if len(looks) == 1 else None)
    look = looks[name].strip() if name is not None else ''
    if look and not any(_is_look_header(line) for _, line in _lines(W)):
        closing = next((start for start, line in _lines(W) if (h := HEADER.match(line)) and CLOSING.match(h['label'])), None)
        if closing is not None:
            additions.append({'kind': 'look', 'at': closing, 'text': _look_body(look) + '\n\n', 'look': name})
        else:
            tail.append('look')
            additions.append({'kind': 'look', 'at': end, 'text': '\n\n' + _look_body(look), 'look': name})
    if not _durations(W):
        additions.append({'kind': 'duration', 'at': end, 'text': f'\n\n{_seconds(seconds)}s.'})
        tail.append('duration')
    if not MUSIC.search(W):
        lead = ' ' if tail and tail[-1] == 'duration' else '\n\n'
        additions.append({'kind': 'no_music', 'at': end, 'text': lead + 'No music.'})
    order = {id(a): i for i, a in enumerate(additions)}
    additions.sort(key=lambda a: (a['at'], ORDER[a['kind']], order[id(a)]))
    return apply(W, additions), images, additions


def _bind_added_lines(W: str, additions: list[dict[str, Any]], numbers: Mapping[str, int],
                      media: Mapping[str, str] | None = None, style: str = 'at') -> None:
    """Binding "first" (owner 2026-09-27): the number goes on the tag's first mention in what is sent. When a
    descriptor line the platform adds (`@kel — …`) comes before the writer's first mention, that line is the first
    mention: the number moves onto it and the writer's mention stays bare (re-audit 2026-09-27)."""
    first: dict[str, int] = {}
    for m in TAG.finditer(W):
        first.setdefault(m[1], m.start())
    for addition in [a for a in additions if a['kind'] == 'descriptor']:
        listed = addition.get('tags') or ([addition['tag']] if addition['text'].startswith('\n@') else [])
        moved = {tag: numbers[tag] for tag in listed if tag in numbers and addition['at'] <= first[tag]}
        if not moved:
            continue
        for tag, n in moved.items():
            addition['text'] = addition['text'].replace(f'@{tag} — ', f'{_token((media or {}).get(tag), n, style)} @{tag} — ', 1)
            additions[:] = [a for a in additions if not (a['kind'] == 'bind' and a['tag'] == tag)]
        addition['numbers'] = moved
        videos = {tag: 'video' for tag in moved if (media or {}).get(tag) == 'video'}
        if videos:
            addition['media'] = videos


def apply(W: str, additions: Sequence[Mapping[str, Any]]) -> str:
    """W with every addition inserted at its offset, in the listed order."""
    parts, at = [], 0
    for addition in additions:
        offset = addition['at']
        if not isinstance(offset, int) or offset < at or offset > len(W):
            raise ValueError('additions must be in order and inside the writer text')
        parts += [W[at:offset], addition['text']]
        at = offset
    parts.append(W[at:])
    return ''.join(parts)


def check(W: str, additions: Sequence[Mapping[str, Any]], sent: str, images: Sequence[Mapping[str, Any]],
          found: Mapping[str, Any]) -> list[str]:
    """Why `sent` is not exactly W plus allowed additions (empty when it is). `found` comes from `constants`."""
    problems: list[str] = []
    try:
        if apply(W, additions) != sent:
            problems.append('what was sent is not the writer text plus the additions')
    except (KeyError, TypeError, ValueError):
        problems.append('the additions overlap, are out of order or fall outside the writer text')
    spans = [(m.start(), m.end()) for m in TAG.finditer(W)]
    kind_of = lambda item: 'video' if item.get('media') == 'video' else 'image'  # noqa: E731
    by_number = {(kind_of(image), image.get('n')): image.get('tag') for image in images}
    for kind in ('image', 'video'):
        listed = [image.get('n') for image in images if kind_of(image) == kind]
        if listed != list(range(1, len(listed) + 1)):
            problems.append(f'{kind}s must be numbered @{_label(kind)}1..@{_label(kind)}N, one per tag')
    if len(set(by_number.values())) != len(images):
        problems.append('images must be numbered @Image1..@ImageN, one per tag')
    used: set[Any] = set()
    for addition in additions:
        kind, at, text = addition.get('kind'), addition.get('at'), addition.get('text')
        if kind not in ORDER:
            problems.append(f'unknown addition kind {kind!r}')
            continue
        if not isinstance(at, int) or any(start < at < stop for start, stop in spans):
            problems.append(f'a {kind} addition splits a writer @tag')
        if text not in _renders(kind, addition, found):
            problems.append(f'a {kind} addition is not from its allowed set: {text!r}')
        if kind == 'bind':
            n, tag = addition.get('n'), addition.get('tag')
            token = next((W[start:stop] for start, stop in spans if start == at), None)
            if by_number.get((kind_of(addition), n)) != tag or token != f'@{tag}':
                problems.append(f'@{_label(addition.get("media"))}{n} must sit on its own @{tag}')
            used.add((kind_of(addition), n))
    described: dict[str, int] = {}
    for addition in additions:
        if addition.get('kind') != 'descriptor':
            continue
        for tag in addition.get('tags') or [addition.get('tag')]:
            described[str(tag)] = described.get(str(tag), 0) + 1
            if _described(W, str(tag)):
                problems.append(f'a descriptor was added for @{tag}, which the writer described')
        for tag, n in (addition.get('numbers') or {}).items():
            number_kind = 'video' if (addition.get('media') or {}).get(tag) == 'video' else 'image'
            if by_number.get((number_kind, n)) != tag:
                problems.append(f'@{_label(number_kind)}{n} on an added line must be the number of @{tag}')
            used.add((number_kind, n))
    problems += [f'more than one descriptor for @{tag}' for tag, n in described.items() if n > 1]
    # The tail additions come at most once each, and only when build would add them (bug hunt 2026-09-27).
    counts = {kind: sum(1 for a in additions if a.get('kind') == kind) for kind in ('look', 'duration', 'no_music')}
    problems += [f'more than one {kind} addition' for kind, n in counts.items() if n > 1]
    if counts['duration'] and _durations(W):
        problems.append('a duration was added although the writer stated one')
    if counts['no_music'] and MUSIC.search(W):
        problems.append('"No music." was added although the writer wrote about music')
    if counts['look'] and any(_is_look_header(line) for _, line in _lines(W)):
        problems.append('a look was added although the writer wrote one')
    if used != set(by_number):
        problems.append('every image must be bound to at least one writer @tag, and no bind may point at a missing image')
    return problems


def _states(text: str) -> set[str]:
    return {STATES[w] for w in re.findall(r'[a-z]+', text.lower()) if w in STATES}


def _stated_seconds(W: str) -> tuple[float, float, bool] | None:
    """The longest duration the writer stated: (low, high, approximate range) of the match that ends last."""
    best = None
    for m in _durations(W):
        numbers = [float(n) for n in re.findall(r'\d+(?:\.\d+)?', m[0])]
        ranged = bool(re.search(r'–|-|to', m[0]))
        low, high = (numbers[0], numbers[-1]) if ranged else (numbers[-1], numbers[-1])
        approximate = ranged and (W[max(0, m.start() - 1):m.start()] == '~' or ' to ' in m[0])
        if best is None or high > best[1]:
            best = (low, high, approximate)
    return best


def advice(W: str, assets: Mapping[str, Mapping[str, Any]], seconds: float) -> list[str]:
    """What HF's practice (lens 9 of 12, timed beats 6 of 12) would have written and this prompt did not; the writer decides, nothing is refused."""
    notes = []
    if not LENS.search(W):
        notes.append('No lens stated (mm or FOV°): 9 of 12 HF projects state the lens in the prompt')
    if not TIMED_BEATS.search(W):
        notes.append('No timed beats (e.g. 0-4s | …): 6 of 12 HF projects time the action in the prompt')
    stated = _stated_seconds(W)
    if stated is not None:
        low, high, approximate = stated
        if not (seconds == high or approximate and low <= seconds <= high):
            said = f'{_seconds(low)}–{_seconds(high)}' if low != high else _seconds(high)
            notes.append(f'The prompt states a duration of {said} s; the card asks for {_seconds(seconds)} s')
    for tag in tags(W):
        if tag not in assets:
            extra = ' (the platform numbers the images itself)' if re.fullmatch(r'Image\d+', tag) else ''
            notes.append(f'@{tag} is not a selected asset of this shot{extra}')
            continue
        descriptor = assets[tag].get('descriptor')
        if not isinstance(descriptor, str):
            continue
        lines = [line for _, line in _lines(W) if any(m[1] == tag for m in TAG.finditer(line))
                 and len(WORD.findall(TAG.sub(' ', line))) >= 4]
        registry, written = _states(descriptor), set().union(*map(_states, lines)) if lines else set()
        if registry and written and registry != written:
            notes.append(f"@{tag}: the registry says {', '.join(sorted(registry))}; the writer's line says "
                         f"{', '.join(sorted(written))}")
    return notes
