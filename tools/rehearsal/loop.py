"""The owner-free test loop: fresh test projects on the rehearsal copy, every function.

Usage (prepare the copy first: tools/rehearsal/rehearse.py prepare --studio <studio> --code-dir <dir>):
  <studio>/venv/bin/python tools/rehearsal/loop.py setup|t1|t2|t3|t4|t5|t6|all --studio <studio> [--scratch]
  --scratch writes the evidence to <studio>/tmp instead of <studio>/evidence.

Each round builds new SIMTEST projects through the agent CLI only (the agent credential is issued by prepare, the way
the operator issues the live one), shoots on fal drafts through the fake fal, makes asset images through the fake
apilio, and plays the owner on the real desk in headless Chrome. The test story is the platform's synthetic one (a
blue cart passes a red marker) plus a two-person dialogue scene; nothing comes from a real film. Each check is one
line of evidence; the round passes when every check passes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from tools.rehearsal.flow import Rehearsal  # noqa: E402

STORY = ('A blue delivery cart travels east along a straight road. It begins west of a red roadside marker, passes the '
         'marker, and continues east. The next view keeps the cart east of the marker; it does not reverse. No one speaks.')
EXPECTED = ('The blue cart starts left of the red marker and finishes right of it. The audience sees the pass.',
            'The blue cart is still right of the marker and continues right. The audience understands it kept going.')
DIALOGUE = ('Two workers meet at the marker at dusk. Lin asks whether the cart came through; Su says it passed an hour ago and '
            'turns away before Lin can ask more.')
TITLES = {'original': 'SIMTEST 原创 · 送货车', 'recreation': 'SIMTEST 复刻 · 送货车'}
LABELS = ('S02-010A', 'S02-020A')


def template() -> dict:
    return json.loads((HERE.parents[1] / 'production/templates/card.json').read_text())


def cart_card(n: int, label: str) -> dict:
    c = template()
    c['shot'] = label
    c['The material'].update({'the location and INT/EXT with the asset that covers it': 'EXT @loc_lane', 'the time of day': 'day',
                              'props and vehicles with tags': ['@bluecart'], 'the running time in seconds': 5,
                              'the complexity — simple, medium or complex': 'simple', 'the action in one to three sentences': EXPECTED[n]})
    c['Direction'].update({'the goal of the shot in one line': ['蓝色送货车从红色路标左边开到右边。', '送货车已经在路标右边，继续往右开。'][n],
                           'The dramaturgy — what changed between the start and the end': EXPECTED[n],
                           'The blocking relative to the camera': 'The marker is at road center; camera stays south.',
                           'end state': 'The blue cart is east of the red marker.', 'expected visible performance': EXPECTED[n]})
    c['Camera'] = {'shot size': 'Wide', 'movement': 'Locked off', 'lens': '35mm', 'angle': 'Eye level'}
    c['ACTING TASK'] = {}
    c['Edit']['how this shot hooks into the next one'] = 'Keep the blue cart east of the red marker.'
    c['_production'] = {'model': 'fal_seedance_2_5', 'aspect_ratio': '16:9', 'resolution': '480p'}  # fal draft route
    return c


class Loop:
    def __init__(self, r: Rehearsal) -> None:
        self.r = r

    # ------------------------------------------------------------ helpers
    def draft(self, pid: str, kind: str, path: str, content, deps=()) -> dict:  # noqa: ANN001
        code, made = self.r.cli('draft', pid, body={'idempotency_key': f'{path}-{self.r.run}'.replace('/', ':'), 'expected_revision': 0,
                                                    'kind': kind, 'logical_path': path, 'content': content, 'dependencies': list(deps)})
        assert code == 0, made
        return made['object_ref']

    def upload(self, pid: str, key: str, raw: bytes, media_type: str, suffix: str) -> dict:
        path = self.r.studio / f'tmp/rehearsal-{key}{suffix}'
        path.write_bytes(raw)
        meta = {'idempotency_key': f'{key}-{self.r.run}', 'logical_path': f'uploads/{key}{suffix}', 'media_type': media_type,
                'byte_length': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        code, made = self.r.cli('upload', pid, str(path), body=meta)
        assert code == 0, made
        return made['object_ref']

    def manuals(self, pid: str) -> str:
        code, taken = self.r.cli('playbook', pid, '--dir', str(self.r.studio / f'tmp/rehearsal-manuals-{pid[-6:]}'))
        assert code == 0, taken
        return taken['playbook_version']

    def image_asset(self, pid: str, tag: str, role: str, category: str, description: str, recipe: str,
                    descriptor: str | None = None) -> dict:
        """An element the HF way: the agent writes LIRA prose, the platform makes the image through apilio, the agent
        binds the image to the element."""
        version = self.manuals(pid)
        definition = {'recipe': recipe, 'description': description, 'visual_treatment': 'photoreal',
                      'playbook_version': version, 'change_note': 'First version of this element.',
                      'descriptor': descriptor or f'The {tag.replace("_", " ")} of this story.'}
        asset = self.draft(pid, 'asset', f'assets/{tag}.json', {'type': 'asset', 'role': role, 'tag': '@' + tag, 'category': category,
                                                                'definition': definition})
        code, method = self.r.cli('select-method', pid, body={'idempotency_key': f'm-{tag}-{self.r.run}', 'expected_revision': asset['revision'],
                                  'target': asset, 'method_id': 'mvgp-image-generate-v1', 'rationale': 'A new element image.'})
        assert code == 0, method
        code, cand = self.r.cli('prepare', pid, body={'idempotency_key': f'p-{tag}-{self.r.run}', 'expected_revision': asset['revision'],
                                'target': asset, 'task': 'image', 'method_selection': method['object_ref'], 'inputs': []})
        assert code == 0, cand
        code, job = self.r.cli('submit', pid, body={'idempotency_key': f's-{tag}-{self.r.run}', 'expected_revision': 1,
                               'candidate_id': cand['object_ref']['object_id']})
        assert code == 0, job
        done = self.r.wait(lambda: (lambda j: j if j['body'].get('state') in ('succeeded', 'failed', 'unknown') else None)(
            self.r.current(pid, job['object_ref']['object_id'])), limit=120, every=2)
        assert done and done['body']['state'] == 'succeeded', done and done['body']
        image = done['body']['result']
        current = self.r.current(pid, asset['object_id'])
        code, bound = self.r.cli('revise', pid, asset['object_id'], body={
            'idempotency_key': f'b-{tag}-{self.r.run}', 'expected_revision': current['revision'], 'kind': 'asset',
            'logical_path': f'assets/{tag}.json', 'content': {**current['body']['content'], 'media_refs': [image]},
            'dependencies': [image]})
        assert code == 0, bound
        return {'asset': bound['object_ref'], 'image': image}

    # ------------------------------------------------------------ T1: projects through the agent CLI only
    def t1(self) -> dict[str, str]:
        films = {}
        for branch, title in TITLES.items():
            code, made = self.r.cli('create-project', body={'idempotency_key': f'simtest-{branch}-{self.r.run}', 'expected_revision': 0,
                                                             'title': f'{title} {self.r.run}', 'branch': branch, 'brief': STORY})
            self.r.check(f'T1_{branch}_agent_creates_the_project', code == 0, made if code else made['project_id'])
            films[branch] = made['project_id']
        for branch, pid in films.items():
            budgets = {k: self.r.budget(pid, k) for k in ('fal_owner', 'apilio_owner_10466', 'hf_owner')}
            self.r.check(f'T1_{branch}_funded_on_creation_without_an_operator',
                         budgets['fal_owner'] and budgets['apilio_owner_10466'] and budgets['hf_owner'], budgets)
            # these two films run the fal sample path (T2–T7), so the owner turns 样片模式 on;
            # the folder film below shoots on Higgsfield, the default.
            on, errors = self.r.owner_switch(pid, self.title(pid), 'sample_mode', True)
            self.r.check(f'T1_{branch}_owner_turns_on_sample_mode_on_the_desk', on is True and not errors, {'on': on, 'errors': errors})
            code, tree = self.r.http('GET', f'/v1/projects/{pid}/project-tree')
            names = {f['name'] for f in tree.get('folders', [])}
            self.r.check(f'T1_{branch}_hf_project_skeleton', code == 200 and {'资产', '角色', '地点', '道具', '测试', '成片'} <= names,
                         sorted(names))
            refused = self.r.agent_cannot_decide(pid)
            self.r.check(f'T1_{branch}_an_agent_bearer_cannot_decide_for_the_owner', refused == 403, refused)
        self.r.save(films=films)
        self.t1_folder()
        return films

    # ------------------------------------------------------------ T1: the folder flow
    def t1_folder(self) -> None:
        """An HF-shaped folder (production/FOLDER.md) with 3 assets and 3 shots, made into takes on the desk and a
        log.md with at most six agent commands: open, image, quote, shoot, pull (push rides inside the others)."""
        root = self.r.studio / f'tmp/rehearsal-films-{self.r.run}/SIMTEST 文件夹 · 送货车'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'brief.md').write_text('# Logline\n' + STORY + '\n\n# About\nA rehearsal film made from the folder.\n')
        calls: list[str] = []
        def mvgp(*args: str) -> tuple[int, dict]:
            calls.append(args[0])
            return self.r.cli(*args)
        code, opened = mvgp('open', str(root))
        pid = opened.get('project_id') if code == 0 else None
        self.r.check('T1_folder_open_creates_the_project_and_fetches_the_manuals', code == 0 and bool(pid)
                     and (root.parent / '.manuals' / str(opened['manuals']['version']) / 'writer.md').exists(), opened)
        if not pid:
            return
        (root / 'script.md').write_text('EXT. STRAIGHT ROAD - DAY\n\n' + STORY + '\n\n' + DIALOGUE + '\n')
        (root / 'registry.md').write_text(
            '## Looks\n### day\nPhotoreal midday, hard sun from the south, dry grass, fine 35mm grain.\n\n'
            '## @bluecart · prop\na small blue delivery cart with two wheels and a flat bed\n\n'
            '## @loc_lane · location\na straight country road with a red roadside marker at its centre\n\n'
            '## @lin · character\nLin, a delivery worker in her thirties, faded blue work jacket, short black hair\n\n'
            '## @road_previs · location\na grey-box previs of the cart passing the marker, left to right\n')
        for kind, tag, text in (('PROPS', '@bluecart', ('A small blue delivery cart with two wheels and a flat bed, shown on a '
                                 'neutral grey background from the side and three-quarter front.')),
                                ('LOCATIONS', '@loc_lane', ('A straight country road at midday seen from the south side, a red '
                                 'roadside marker at the centre, dry grass on both verges, a wide 16:9 plate with no people.')),
                                ('CHARACTERS', '@lin', ('Character sheet of Lin, a delivery worker in a faded blue work jacket: '
                                 'grey 16:9 background, front and back full body and a large three-quarter portrait.'))):
            (root / 'ASSETS' / kind / f'{tag}.md').write_text(text + '\n')
        # a hand-placed previs clip, the Higgsfield video reference (passport-rush's Blender blockout).
        (root / 'ASSETS' / 'LOCATIONS' / '@road_previs.md').write_text('Grey-box previs of the cart passing the marker, made by hand.\n')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=gray:s=640x360:r=24:d=4', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(root / 'ASSETS' / 'LOCATIONS' / '@road_previs.mp4')], check=True)
        scene = root / 'SCENE 01 - ROAD'
        scene.mkdir(exist_ok=True)
        shots = [('010', 5, '蓝色送货车从红色路标左边开到右边', '@bluecart', EXPECTED[0]),
                 ('020', 5, '送货车已经在路标右边，继续往右开', '@bluecart', EXPECTED[1]),
                 ('030', 6, '林站在路标旁看着车开远', '@lin', 'Lin stands by the red marker and watches the cart go.')]
        sections = []
        for number, seconds, goal, tag, action in shots:
            prompt = self.r.prompt_for({'The material': {'the action in one to three sentences': action,
                                                         'the running time in seconds': seconds,
                                                         'the location and INT/EXT with the asset that covers it': 'EXT @loc_lane',
                                                         **({'everyone in frame with their tags and state variants': [tag]}
                                                            if tag == '@lin' else {'props and vehicles with tags': [tag]})},
                                        'Direction': {'the goal of the shot in one line': goal}})
            if number == '010':
                prompt += '\n\n@road_previs — motion, camera and timing reference only; the grey-box look is not inherited.'
            sections.append(f'## {number} · {seconds}s · {goal}\nlook: day\n\n{prompt}\n')
        (scene / 'shotlist.md').write_text('The cart passes the marker; Lin watches it go.\n\n' + '\n'.join(sections))
        code, made = mvgp('image', str(root), '@bluecart', '@loc_lane', '@lin', '--wait-seconds', '300')
        landed = [i['tag'] for i in made.get('images', []) if i.get('state') == 'succeeded'] if code == 0 else []
        self.r.check('T1_folder_image_lands_every_asset_picture_in_ASSETS', sorted(landed) == ['@bluecart', '@lin', '@loc_lane']
                     and all((root / 'ASSETS' / k / f'{t}.png').exists() for k, t in (('PROPS', '@bluecart'),
                             ('LOCATIONS', '@loc_lane'), ('CHARACTERS', '@lin'))), made)
        code, quoted = mvgp('quote', str(root))
        self.r.check('T1_folder_quote_prices_every_shot_and_spends_nothing', code == 0 and len(quoted.get('cards', [])) == 3
                     and not quoted.get('advice') and not self.r.objects(pid, 'batch'), quoted)
        review = root.parent / 'review.json'
        review.write_text(json.dumps({'reviewer': 'rehearsal reviewer', 'notes': [
            {'line': 'cinedance:149', 'note': 'The audit list must not be in the prompt.', 'answer': 'Checked: it is not.'}]}))
        code, ordered = mvgp('shoot', str(root), '01', '--review', str(review))
        self.r.check('T1_folder_shoot_orders_the_three_shots_with_the_review', code == 0 and ordered.get('shots') == ['01:010', '01:020', '01:030']
                     and all(c.get('reason') is None for c in ordered.get('order', {}).get('cards', [])), ordered)
        labels = [f'S01-{n}' for n, *_ in shots]
        offered = self.r.wait(lambda: all(self.r.offers(pid, label) for label in labels) or None, limit=160)
        code, _ = mvgp('pull', str(root))
        log = (root / 'log.md').read_text() if (root / 'log.md').exists() else ''
        self.r.check('T1_folder_takes_reach_the_desk_and_pull_writes_the_log', offered is not None and code == 0
                     and all(f'## {label} ·' in log and '待选' in log for label in labels) and 'Reviewer: rehearsal reviewer' in log,
                     {'offered': offered is not None, 'log': log[:400]})
        self.r.check('T1_folder_flow_takes_at_most_six_agent_commands', len(calls) <= 6, calls)
        self.hf_route(pid, {f'S01-{n}': seconds for n, seconds, *_ in shots})
        self.r.save(folder_film=pid)

    def hf_route(self, pid: str, seconds: dict[str, int]) -> None:
        """a film not in 样片模式 shoots on Higgsfield at 1080p. Every create the fake CLI got
        carries a candidate's prompt byte for byte (the writer text plus the allowed additions), at 1080p, with the
        reference images the candidate froze; the takes settle at 12 credits per second and hold nothing after."""
        creates_log = self.r.paths.root / 'fake-hf' / 'hf-home' / 'creates.jsonl'
        creates = [json.loads(line) for line in creates_log.read_text().splitlines()] if creates_log.exists() else []
        candidates = {c['body']['compilation']['prompt']: c['body']['compilation'] for c in self.r.objects(pid, 'candidate')}
        jobs = [j for j in self.r.objects(pid, 'job')]
        problems = []
        with_video: set[bool] = set()
        for create in creates:
            flags = create['flags']
            candidate = candidates.get(flags.get('--prompt'))
            if candidate is None:
                problems.append('a create whose prompt is no candidate prompt')
                continue
            if flags.get('--resolution') != '1080p' or create['job_type'] != 'seedance_2_5':
                problems.append(f"{create['job_type']} {flags.get('--resolution')}")
            frozen = candidate.get('references') or []
            refs = len(frozen)
            videos = sum(1 for r in frozen if str(r.get('media_type', '')).startswith('video/'))
            if (create['references'].get('--image-references', 0), create['references'].get('--video-references', 0)) != (refs - videos, videos):
                problems.append(f"{create['references']} references for a candidate with {refs - videos} images and {videos} videos")
            if videos and '<<<video_1>>> @road_previs' not in flags.get('--prompt', ''):
                problems.append('a video reference without <<<video_1>>> on its tag')
            if '@Image' in flags.get('--prompt', '') or '@Video' in flags.get('--prompt', ''):
                problems.append('a fal-style @ImageN / @VideoN on the Higgsfield route')
            if (flags.get('--mode') == 't2v') != (refs == 0):
                problems.append(f"mode {flags.get('--mode')} with {refs} references")
            with_video.add(bool(videos))
        self.r.check('T8_folder_film_shoots_on_higgsfield_at_1080p_with_the_writer_text', len(creates) == 4 * len(seconds)
                     and not problems, {'creates': len(creates), 'problems': problems[:5]})
        self.r.check('T8_a_previs_clip_goes_to_higgsfield_as_a_video_reference', with_video == {True, False}, sorted(with_video))
        hf = self.r.budget(pid, 'hf_owner') or {}
        want = 12 * 4 * sum(seconds.values())
        self.r.check('T8_higgsfield_takes_settle_at_12_credits_a_second_with_no_open_hold',
                     hf.get('spent') == want and hf.get('reserved') == 0, {'budget': hf, 'expected_spent': want})
        fal = self.r.budget(pid, 'fal_owner') or {}
        self.r.check('T8_nothing_of_this_film_went_to_fal', not fal.get('spent') and not fal.get('reserved'), fal)

    # ------------------------------------------------------------ T2: pre-production, images through apilio
    def t2(self, films: dict[str, str]) -> None:
        for branch, pid in films.items():
            deps = []
            if branch == 'recreation':
                clip = self.r.studio / 'tmp/rehearsal-source.mp4'
                subprocess.run([f'{self.r.ffmpeg}/ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24:duration=5',
                                '-f', 'lavfi', '-i', 'sine=frequency=330:duration=5', '-c:a', 'aac', '-shortest', '-pix_fmt', 'yuv420p', str(clip)],
                               check=True)
                source = self.upload(pid, f'source-{self.r.run}', clip.read_bytes(), 'video/mp4', '.mp4')
                observation = self.observe(pid, source)
                self.r.check('T2_recreation_the_reader_watches_the_source', observation is not None, observation)
                deps = [self.draft(pid, 'source-understanding', 'source/understanding.json', {
                    'type': 'source-understanding', 'source': source, 'start_seconds': 0.0, 'end_seconds': 4.5, 'observation': observation,
                    'observed_facts': ['A fixed camera frames a straight road.', 'Something passes from frame-left to frame-right.'],
                    'uncertain_interpretations': ['Nothing about intent can be read from the pattern.'],
                    'adaptation_scope': 'Keep the left-to-right pass and the fixed camera; the cart and marker are ours.'}, [source, observation])]
            script = self.draft(pid, 'script', 'story/script.fountain', STORY, deps)
            expected = self.draft(pid, 'expectation', 'story/expected.md', '\n'.join(EXPECTED), [script])
            scene = self.draft(pid, 'scene', 'scenes/S02.md', 'S02 · EXT · Straight road · DAY\n' + STORY +
                               '\n## GEO SPATIAL LAYOUT\nCamera stays south. East is frame-right.\n## ACTIVE REFERENCES\n@loc_lane @bluecart\n',
                               [script, expected])
            lane = self.image_asset(pid, 'loc_lane', 'world', 'environment', 'A straight country road at midday seen from the south side, '
                                    'a red roadside marker at the centre, dry grass on both verges, a wide 16:9 plate with no people.', 'location-angle')
            cart = self.image_asset(pid, 'bluecart', 'visual', 'prop', 'A small blue delivery cart with two wheels and a flat bed, '
                                    'shown on a neutral grey background from the side and three-quarter front.', 'diagram')
            self.draft(pid, 'asset', 'assets/selection.json', {'type': 'asset-selection', 'target': scene,
                                                              'selected': {'world': lane['asset'], 'visual': cart['asset']}},
                       [lane['asset'], cart['asset']])
            for n, label in enumerate(LABELS):
                self.draft(pid, 'shot', f'shots/{label}/card.json', cart_card(n, label), [scene, *deps])
            code, tree = self.r.http('GET', f'/v1/projects/{pid}/project-tree')
            filed = {i['folder'] for i in tree.get('items', []) if i['kind'] == 'image'}
            spent = self.r.budget(pid, 'apilio_owner_10466')
            # Images settle from their token counts; a source reading keeps its hold for the operator (production/reader.py:6).
            readings = len([o for o in self.r.objects(pid, 'observation') if o['author'] == 'reader_service'])
            self.r.check(f'T2_{branch}_element_images_made_through_apilio_land_in_assets',
                         code == 200 and {'assets/locations', 'assets/props'} <= filed and spent['spent'] > 0
                         and spent['reserved'] == 500000 * readings, {'folders': sorted(filed), 'apilio': spent, 'readings': readings})

    def observe(self, pid: str, media: dict) -> dict | None:
        code, job = self.r.cli('observe', pid, body={'idempotency_key': f'o-{self.r.run}', 'expected_revision': media['revision'],
                               'media_id': media['object_id'], 'questions': ['What moves, and which way?'], 'reader': 'video',
                               'source_offset_seconds': 0.0, 'time_scale': 1.0})
        if code != 0:
            return None
        found = self.r.wait(lambda: [o for o in self.r.objects(pid, 'observation') if o['author'] == 'reader_service'
                                     and (o['body'].get('source') or {}).get('object_id') == media['object_id']], limit=120, every=2)
        return self.r.ref(found[-1]) if found else None

    # ------------------------------------------------------------ T3: shoot, pick, 1080p, film
    def t3(self, films: dict[str, str]) -> None:
        for branch, pid in films.items():
            title = self.title(pid)
            if any(r['body'].get('purpose') == 'final' and r['body'].get('state') == 'confirmed' for r in self.r.objects(pid, 'decision-request')):
                continue  # this project's round already ran to the confirmed film
            for label in LABELS:
                if self.r.offers(pid, label):
                    continue  # already shot in an earlier, interrupted run of this round
                code, stages = self.r.write_and_shoot(pid, label, self.r.studio / f'tmp/rehearsal-manuals-{pid[-6:]}')
                self.r.check(f'T3_{branch}_{label}_written_card_is_shot', code == 200 and all(s[1] is None for s in stages.values()), stages)
            offered = {l: self.r.wait(lambda l=l: self.r.offers(pid, l), limit=120) for l in LABELS}
            self.r.check(f'T3_{branch}_four_480p_samples_reach_the_desk', all(offered.values()),
                         {l: len((o or [{}])[0].get('body', {}).get('evidence', {}).get('takes', [])) for l, o in offered.items()})
            code, feed = self.r.http('GET', f'/v1/projects/{pid}/review-feed')
            states = {v['state'] for v in feed.get('completions', {}).values()}
            self.r.check(f'T3_{branch}_samples_are_fal_drafts_on_the_desk', states == {'draft'}, sorted(states))
            code, status, errors = self.r.owner_pick(pid, title, LABELS[0], 0)
            self.r.check(f'T3_{branch}_owner_picks_{LABELS[0]}', code == 200 and '定了' in status and not errors, {'status': status, 'errors': errors})
            sentence = '送货车过路标太快了，要看清它从左边开到右边。'
            code, errors = self.r.owner_rebatch(pid, title, LABELS[1], sentence)
            code2, reason = self.r.owner_reason(pid, LABELS[1])
            self.r.check(f'T3_{branch}_agent_reads_the_owners_sentence', code == 200 and code2 == 0 and sentence in (reason or ''), reason)
            card = self.r.shot_card(pid, LABELS[1])
            production = json.loads(json.dumps(card['body']['content']['_production']))
            # The workbench patches only the section that failed (cully:25): the ACTION TIMING line of the prompt.
            head, _, rest = production['prompt'].partition('ACTION TIMING\n')
            production['prompt'] = (head + 'ACTION TIMING\n0.0–5.0s — The cart rolls slowly from left of the marker to right of it; '
                                    'the pass fills the middle second.\n' + rest.partition('\n')[2])
            code, patched = self.r.cli('patch', pid, body={'idempotency_key': f'rs-{self.r.run}-{pid[-4:]}', 'expected_revision': card['revision'],
                                       'target': self.r.ref(card), 'creative_path': ['content', '_production'], 'value': production,
                                       'reason': f'Owner: {sentence} → ACTION TIMING: a slower pass.'})
            before = len(self.r.offers(pid, LABELS[1]))
            code, fired = self.r.order(pid, [self.r.shot_card(pid, LABELS[1])], f'reshoot-{self.r.run}-{pid[-4:]}')
            again = self.r.wait(lambda: len(self.r.offers(pid, LABELS[1])) > before or None, limit=120)
            self.r.check(f'T3_{branch}_one_patch_then_a_new_batch', again is not None, {'fired': fired.get('cards')})
            code, status, errors = self.r.owner_pick(pid, title, LABELS[1], 1)
            self.r.check(f'T3_{branch}_owner_picks_from_the_new_batch', code == 200 and '定了' in status, status)
            ready = self.r.wait(lambda: (lambda f: f if f and all(v['state'] == 'ready' for k, v in f.items() if k in self.picked(pid)) and
                                         len([1 for k in self.picked(pid) if k in f]) == 2 else None)(
                                             self.r.http('GET', f'/v1/projects/{pid}/review-feed')[1].get('completions')), limit=120)
            self.r.check(f'T3_{branch}_each_pick_is_completed_to_1080p_once', ready is not None and self.completions(pid) == 2,
                         {'completion_jobs': self.completions(pid)})
            finals = self.r.wait(lambda: [r for r in self.r.objects(pid, 'decision-request') if r['body'].get('purpose') == 'final'
                                          and r['body'].get('state') == 'pending'], limit=160)
            cut = self.film_media(pid)
            self.r.check(f'T3_{branch}_the_film_is_cut_at_1920x1080_from_the_1080p_takes', finals is not None and cut is not None
                         and (cut['width'], cut['height']) == (1920, 1080) and cut['from_completions'], cut)
            seen = self.r.owner_film(pid, title)
            confirmed = any(r['body'].get('purpose') == 'final' and r['body'].get('state') == 'confirmed' for r in self.r.objects(pid, 'decision-request'))
            self.r.check(f'T3_{branch}_owner_watches_and_confirms_the_film', seen['enabled'] and seen['code'] == 200 and confirmed and not seen['errors'],
                         seen)
            fal = self.r.budget(pid, 'fal_owner')
            self.r.check(f'T3_{branch}_fal_costs_settle_with_no_open_hold', fal['spent'] > 0 and fal['reserved'] == 0, fal)
            code, tree = self.r.http('GET', f'/v1/projects/{pid}/project-tree')
            full = [i for i in tree.get('items', []) if i['name'].endswith('· 正片')]
            self.r.check(f'T3_{branch}_project_page_shows_the_正片_beside_its_sample', len(full) == 2, [i['name'] for i in full])

    def title(self, pid: str) -> str:
        return self.r.current(pid, pid)['body'].get('title', '')

    def picked(self, pid: str) -> set[str]:
        code, feed = self.r.http('GET', f'/v1/projects/{pid}/review-feed')
        return {v['details']['take']['object_id'] for v in feed.get('picks', {}).values() if v['details'].get('take')}

    def completions(self, pid: str) -> int:
        return len([i for i in self.r.objects(pid, 'dispatch-intent') if i['body'].get('operation') == 'complete-draft'])

    def film_media(self, pid: str) -> dict | None:
        cuts = [c for c in self.r.objects(pid, 'cut') if c['author'] == 'cut_service']
        films = [m for m in self.r.objects(pid, 'media') if m['author'] == 'cut_service' and m['body'].get('source_cut')]
        if not cuts or not films:
            return None
        probe = films[-1]['body'].get('probe') or {}
        takes = [self.r.current(pid, s['take']['object_id']) for s in cuts[0]['body']['segments']]
        return {'width': probe.get('width') or probe.get('display_width'), 'height': probe.get('height') or probe.get('display_height'),
                'from_completions': all('completes' in t['body'] for t in takes)}

    # ------------------------------------------------------------ T4: the desk's other buttons (original project)
    def t4(self, films: dict[str, str]) -> None:
        pid, title, r = films['original'], self.title(films['original']), self.r
        run = r.run
        # A third card, shot and then answered with 都不行 and a sentence the agent reads back.
        scene = next(o for o in r.objects(pid, 'scene'))
        if r.shot_card(pid, 'S02-030A') is None:
            c = cart_card(1, 'S02-030A')
            c['Direction']['the goal of the shot in one line'] = '送货车停在路标右边。'
            self.draft(pid, 'shot', 'shots/S02-030A/card.json', c, [r.ref(scene)])
        if not r.offers(pid, 'S02-030A'):
            r.write_and_shoot(pid, 'S02-030A', r.studio / f'tmp/rehearsal-manuals-{pid[-6:]}')
            r.wait(lambda: r.offers(pid, 'S02-030A'), limit=120)
        b, page, errors = r.browser([pid])
        try:
            r.open_project(page, title)
            c = r.card(page, 'S02-030A')
            c.wait_for(timeout=30000)
            c.get_by_role('button', name='都不行', exact=True).click()
            box = c.locator('.note-box input')
            box.fill('车停的位置不对，要停在路标右边一米。')
            with page.expect_response(lambda x: x.url.endswith('/human-decisions'), timeout=30000) as resp:
                box.press('Enter')
            page.wait_for_timeout(1500)
            status = c.locator('.status').inner_text()
        finally:
            b.close()
        code, reason = r.owner_reason(pid, 'S02-030A')
        r.check('T4_reject_all_is_recorded_and_read', resp.value.status == 200 and '都不行' in status and '一米' in (reason or ''),
                {'status': status, 'reason': reason})
        # The confirmed film is picture-locked: the agent reopens exactly what it changes, with a reason.
        lock = next((o for o in r.objects(pid, 'picture-lock') if o['body'].get('state') in ('locked', 'partially-reopened')), None)
        if lock is not None:
            protected = {x['object_id'] for x in lock['body']['protected']}
            wanted = [o for o in [r.shot_card(pid, 'S02-010A'), *r.objects(pid, 'scene'),
                      *[a for a in r.objects(pid, 'asset') if (a['body'].get('content') or {}).get('type') == 'asset-selection']]
                      if o['object_id'] in protected]
            code, reopened = r.cli('reopen', pid, body={'idempotency_key': f'reopen-{run}', 'expected_revision': lock['revision'],
                                   'lock': r.ref(lock), 'targets': [r.ref(o) for o in wanted], 'reason': 'Try a slower pass on S02-010A.'})
            r.check('T4_a_confirmed_film_reopens_with_a_reason', code == 0, {'reopened': len(wanted)})
        # A new batch of a picked shot; pick from it, then 撤销选择: the earlier pick comes back.
        card = r.shot_card(pid, 'S02-010A')
        code, patched = r.cli('patch', pid, body={'idempotency_key': f'edit-010-{run}', 'expected_revision': card['revision'],
                              'target': r.ref(card), 'creative_path': ['content', 'Direction', 'the goal of the shot in one line'],
                              'value': '蓝色送货车慢一点经过红色路标。', 'reason': 'Slower pass, as a second try.'})
        before_offers = len(r.offers(pid, 'S02-010A'))
        r.order(pid, [r.shot_card(pid, 'S02-010A')], f'round2-010-{run}')
        r.wait(lambda: len(r.offers(pid, 'S02-010A')) > before_offers or None, limit=120)
        before = self.picked(pid)
        b, page, errors = r.browser([pid])
        try:
            r.open_project(page, title)
            c = r.card(page, 'S02-010A')
            c.wait_for(timeout=30000)
            c.locator('.takes > .take:not(.source)').nth(2).hover()
            with page.expect_response(lambda x: x.url.endswith('/human-decisions'), timeout=30000):
                c.get_by_role('button', name='用这条', exact=True).nth(0).click()
            c.locator('.status', has_text='定了').wait_for(timeout=20000)
            with page.expect_response(lambda x: x.url.endswith('/human-decisions'), timeout=30000):
                c.get_by_role('button', name='撤销选择', exact=True).click()
            page.wait_for_timeout(2000)
            posts: list[str] = []
            page.on('request', lambda x: posts.append(x.url) if x.method == 'POST' else None)
            page.locator('#toggle-cut').click()
            seg = page.locator('#cut-strip .seg').filter(has_text='S02-010A')
            seg.wait_for(timeout=20000)
            strip = page.locator('#cut-strip .seg').all_inner_texts()
            seg.click()
            page.locator('#cut-takes .l-take').nth(1).click()
            page.wait_for_timeout(800)
            trying = page.locator('#cut-ref').inner_text()
            page.locator('#toggle-cut').click()
            page.locator('#open-settings').click()
            page.locator('#open-record').click()
            page.locator('#record').wait_for(state='visible', timeout=20000)
            page.wait_for_timeout(1500)
            record = page.locator('#record').inner_text()
        finally:
            b.close()
        after = self.picked(pid)
        r.check('T4_withdraw_restores_the_earlier_pick_after_a_card_edit', after == before and not errors,
                {'before': len(before), 'after': len(after), 'errors': errors})
        r.check('T4_film_strip_plays_the_pick_and_trying_is_preview_only',
                not any('未定' in t and 'S02-010A' in t for t in strip) and '试看' in trying
                and not any(u.endswith('/human-decisions') for u in posts), {'strip': strip, 'cut_ref': trying})
        r.check('T4_generation_record_lists_the_takes', 'seedance' in record.lower() or '片子' in record, record[:160])
        # The scene is edited: a written card stays shootable with no rebind (owner-approved:
        # a scene edit never stales a card; the card itself still does).
        scene = next(o for o in r.objects(pid, 'scene'))
        code, done = r.cli('revise', pid, scene['object_id'], body={'idempotency_key': f'scene-{run}', 'expected_revision': scene['revision'],
                           'kind': 'scene', 'logical_path': scene['body']['logical_path'],
                           'content': scene['body']['content'] + '\nLate afternoon light.\n', 'dependencies': scene['body'].get('dependencies', [])})
        card = r.shot_card(pid, 'S02-030A')
        code, patched = r.cli('patch', pid, body={'idempotency_key': f'030-owner-{run}', 'expected_revision': card['revision'],
                              'target': r.ref(card), 'creative_path': ['content', 'Direction', 'the goal of the shot in one line'],
                              'value': '送货车停在路标右边一米。', 'reason': 'Owner: 车停在路标右边一米。'})
        code, fired = r.order(pid, [r.shot_card(pid, 'S02-030A')], f'after-scene-{run}')
        stage = {x['shot']: (x.get('stage'), x.get('reason')) for x in fired.get('cards', [])}
        r.check('T4_a_scene_edit_leaves_the_card_shootable_without_rebinding',
                code == 200 and stage and all(v[1] is None for v in stage.values()), {'scene_revised': done.get('object_ref'), 'order': stage})

    # ------------------------------------------------------------ T5: a two-person dialogue scene
    def t5(self, films: dict[str, str]) -> None:
        pid = films['original']
        people = {tag: self.image_asset(pid, tag, 'visual', 'character', f'Character sheet of {name}, a delivery worker in a faded work '
                                        'jacket: grey 16:9 background, front and back full body and a large three-quarter portrait.',
                                        'base-portrait') for tag, name in (('lin', 'Lin'), ('su', 'Su'))}
        scene = self.draft(pid, 'scene', 'scenes/S03.md', 'S03 · EXT · The marker · DUSK\n' + DIALOGUE +
                           '\n## GEO SPATIAL LAYOUT\nLin west of the marker, Su east. Camera south.\n## ACTIVE REFERENCES\n@loc_lane @lin @su\n')
        lane = next(a for a in self.r.objects(pid, 'asset') if (a['body'].get('content') or {}).get('tag') == '@loc_lane')
        # One selection per element role: two people are two selections (the compiler keys a selection by role).
        self.draft(pid, 'asset', 'assets/selection-s03.json', {'type': 'asset-selection', 'target': scene, 'selected': {
            'world': self.r.ref(lane), 'visual': people['lin']['asset']}}, [self.r.ref(lane), people['lin']['asset']])
        self.draft(pid, 'asset', 'assets/selection-s03-su.json', {'type': 'asset-selection', 'target': scene, 'selected': {
            'visual': people['su']['asset']}}, [people['su']['asset']])
        c = template()
        c['shot'] = 'S03-010A'
        c['The material'].update({'the location and INT/EXT with the asset that covers it': 'EXT @loc_lane', 'the time of day': 'dusk',
                                  'everyone in frame with their tags and state variants': ['@lin', '@su'],
                                  'the action in one to three sentences': 'Lin asks about the cart; Su answers and turns away.',
                                  'the lines verbatim': 'lin: 车过去了吗？\nsu: 一个钟头前就过去了。', 'speakers': ['lin', 'su'],
                                  'the running time in seconds': 8, 'the complexity — simple, medium or complex': 'medium'})
        c['Direction'].update({'the goal of the shot in one line': '林问车，苏答完就转身走。', 'timing mode': 'timed',
                               'The blocking relative to the camera': 'Lin frame-left, Su frame-right, both facing each other.'})
        c['Camera'] = {'shot size': 'Medium two-shot', 'movement': 'Locked off', 'lens': '50mm', 'angle': 'Eye level'}
        c['ACTING TASK'] = {}
        c['_production'] = {'model': 'fal_seedance_2_5', 'aspect_ratio': '16:9', 'resolution': '480p'}
        self.draft(pid, 'shot', 'shots/S03-010A/card.json', c, [scene])
        code, taken = self.r.cli('playbook', pid, '--dir', str(self.r.studio / f'tmp/rehearsal-manuals-{pid[-6:]}'))
        card = self.r.shot_card(pid, 'S03-010A')
        written = self.r.prompt_for(card)
        head, _, rest = written.partition('ACTION TIMING\n')
        written = (head + 'ACTION TIMING\n0.0–3.5s @lin asks "车过去了吗？". CUT TO 3.5–8.0s @su answers "一个钟头前就过去了。" '
                   'and turns away in slow motion.\n' + rest.partition('\n')[2])
        production = {**c['_production'], 'playbook_version': taken['playbook_version'], 'prompt': written}
        code, patched = self.r.cli('patch', pid, body={'idempotency_key': f't5-{self.r.run}', 'expected_revision': card['revision'],
                                   'target': self.r.ref(card), 'creative_path': ['content', '_production'], 'value': production,
                                   'reason': 'Dialogue scene: cuts and a slow-motion beat, no voice asset.'})
        code, stages = self.r.write_and_shoot(pid, 'S03-010A', self.r.studio / f'tmp/rehearsal-manuals-{pid[-6:]}')
        cands = [o for o in self.r.objects(pid, 'candidate') if (o['body'].get('target') or {}).get('object_id') == card['object_id']]
        advice = (cands[-1]['body'].get('compilation', {}).get('authorship') or {}).get('advice', []) if cands else []
        # cuts, slow motion and no voice asset are the writer's choices; nothing craft refuses a take.
        self.r.check('T5_dialogue_with_cuts_and_slow_motion_fires_without_refusal',
                     code == 200 and all(s[1] is None for s in stages.values()), {'stages': stages, 'advice': advice[:6]})
        prompt = cands[-1]['body']['request']['params']['prompt'] if cands else ''
        self.r.check('T5_the_prompt_keeps_the_cut_and_the_lines', 'CUT TO' in prompt and '一个钟头前就过去了' in prompt, prompt[:300])

    # ------------------------------------------------------------ T6: recreation with the reader switch on
    def t6(self, films: dict[str, str]) -> None:
        pid, title = films['recreation'], self.title(films['recreation'])
        code, switches = self.r.http('GET', f'/v1/projects/{pid}/switches')
        on = (switches.get('switches') or switches).get('source_reading')
        self.r.check('T6_recreation_starts_with_the_reader_switch_on', on is True, switches)
        b, page, errors = self.r.browser([pid])
        try:
            self.r.open_project(page, title)
            page.locator('button[data-filter="all"]').click()  # picked shots sit under 全部, not 待审
            card = self.r.card(page, LABELS[0])
            card.wait_for(timeout=30000)
            source = card.locator('.take.source video').get_attribute('src') or ''
            chips = card.locator('.chip.res').all_inner_texts()
        finally:
            b.close()
        self.r.check('T6_the_desk_plays_the_source_segment_beside_the_takes', '#t=0,4.5' in source or '#t=0.0,4.5' in source,
                     {'src': source[-40:], 'chips': chips})
        self.r.check('T6_the_picked_take_shows_as_正片', '正片' in chips, chips)
        seen = self.r.owner_film(pid, title, confirm=False)
        full = [m for m in self.r.objects(pid, 'media') if m['author'] == 'worker_service' and 'completes' in m['body']]
        self.r.check('T6_看成片_plays_the_film_cut_from_the_正片', seen['src'] != '' and len(full) == 2 and not seen['errors'],
                     {'src': seen['src'][-40:], 'completions': len(full)})
        self.sent_is_writer_text(films)

    def sent_is_writer_text(self, films: dict[str, str]) -> None:
        """every prompt fal received is exactly a candidate's writer text plus allowed additions,
        with one image per numbered reference (the fake fal logs every JSON body it receives)."""
        from production import prompt as prompts
        log = self.r.paths.root / 'fal-requests.jsonl'
        bodies = [json.loads(line)['body'] for line in log.read_text().splitlines()] if log.exists() else []
        by_prompt: dict[str, list[dict]] = {}
        for body in bodies:
            if 'prompt' in body:
                by_prompt.setdefault(body['prompt'], []).append(body)
        checked, problems = 0, []
        for pid in films.values():
            sent = {(i['body'].get('candidate') or {}).get('object_id') for i in self.r.objects(pid, 'dispatch-intent')}
            for c in self.r.objects(pid, 'candidate'):
                if c['object_id'] not in sent or c['body'].get('task') not in ('shot', 'stress'):
                    continue
                compiled, wire = c['body'].get('compilation') or {}, c['body']['request']['params']['prompt']
                refs = c['body']['request'].get('references') or []
                received = by_prompt.get(wire, [])
                if not received:
                    problems.append(f"{c['object_id']}: no fal request carries this candidate's prompt")
                if any(len(b.get('image_urls') or []) != len(refs) for b in received):
                    problems.append(f"{c['object_id']}: fal got a different number of images than the prompt numbers")
                if 'writer_text' not in compiled:
                    problems.append(f"{c['object_id']}: prepared without the writer's text")
                    continue
                problems += [f"{c['object_id']}: {p}" for p in prompts.check(
                    compiled['writer_text'], compiled['additions'], wire, [{'n': r.get('n'), 'tag': r.get('tag')} for r in refs],
                    compiled['prompt_constants'])]
                checked += 1
        self.r.check('T6_every_fal_request_is_the_writer_text_plus_allowed_additions', checked > 0 and not problems,
                     {'candidates': checked, 'fal_requests': len(bodies), 'problems': problems[:5]})


    # ------------------------------------------------------------ T7: 再拍一批 without a sentence
    def batch_counts(self) -> dict[str, int]:
        return dict(self.r.db().execute("SELECT project_id, COUNT(*) FROM objects WHERE kind='batch' GROUP BY project_id").fetchall())

    def t7(self, films: dict[str, str]) -> None:
        pid, title, label = films['original'], self.title(films['original']), 'S03-010A'
        # The copy's own answers are older than the cutoff the rehearsal recorded before the worker started.
        before, now = self.r.state().get('old_batches') or {}, self.batch_counts()
        grown = {p: (n, now.get(p, 0)) for p, n in before.items() if p not in films.values() and now.get(p, 0) != n}
        step = 'T7_starting_the_worker_on_the_copy_fires_no_batch_for_old_answers'
        if not before:
            self.r.skip(step, 'no pre-existing batches in this studio; historical reshoot protection needs a populated backup')
        else:
            self.r.check(step, not grown, {'projects': len(before), 'changed': grown})
        offered = self.r.wait(lambda: self.r.offers(pid, label), limit=120)
        card = self.r.shot_card(pid, label)
        def candidates() -> list[dict]:
            return [c for c in self.r.objects(pid, 'candidate') if (c['body'].get('target') or {}).get('object_id') == card['object_id']]
        def batches_of(ids: set[str]) -> list[dict]:
            return [b for b in self.r.objects(pid, 'batch') if {c['candidate']['object_id'] for c in b['body'].get('children', [])} & ids]
        prepared = candidates()
        ids = {c['object_id'] for c in prepared}
        fired = len(batches_of(ids))
        code, errors = self.r.owner_rebatch(pid, title, label, '')
        again = self.r.wait(lambda: len(batches_of(ids)) > fired or None, limit=120)
        latest = max(batches_of(ids), key=lambda b: b['body'].get('children', [{}])[0].get('idempotency_key', ''), default=None)
        self.r.check('T7_a_blank_rebatch_fires_the_same_card_again_with_no_agent_call',
                     offered is not None and code == 200 and not errors and again is not None and len(candidates()) == len(prepared)
                     and self.r.shot_card(pid, label)['revision'] == card['revision'],
                     {'batches_before': fired, 'batches_after': len(batches_of(ids)), 'candidates': [len(prepared), len(candidates())],
                      'latest_takes': len((latest or {}).get('body', {}).get('children', []))})
        back = self.r.wait(lambda: len(self.r.offers(pid, label)) > 0 and (self.r.offers(pid, label)[0]['object_id']
                                                                         != (offered or [{}])[0].get('object_id')) or None, limit=160)
        self.r.check('T7_the_reshot_takes_reach_the_desk', back is not None, {'offered': bool(back)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('steps', nargs='+', choices=['setup', 'up', 't1', 't2', 't3', 't4', 't5', 't6', 't7', 'all', 'down'])
    parser.add_argument('--studio', type=Path, required=True)
    parser.add_argument('--scratch', action='store_true')
    args = parser.parse_args()
    evidence = args.studio / ('tmp' if args.scratch else 'evidence') / 'rehearsal-loop.json'
    r = Rehearsal(args.studio, evidence=evidence)
    sys.path.insert(0, str(r.runtime))
    loop = Loop(r)
    steps = ['setup', 't1', 't2', 't3', 't4', 't5', 't6', 't7'] if 'all' in args.steps else args.steps
    if 'down' in steps:
        r.down()
        return 0
    if 'setup' in steps and evidence.exists():
        evidence.rename(evidence.with_name(f'rehearsal-loop-earlier-{r.run}.json'))
    if 'setup' in steps or 'up' in steps:
        r.up()
        if 'setup' in steps:
            r.save(old_batches=loop.batch_counts())  # the copy's batches when its worker starts (T7)
    try:
        films = r.state().get('films') or {}
        if 't1' in steps:
            films = loop.t1()
        for name in ('t2', 't3', 't4', 't5', 't6', 't7'):
            if name in steps:
                getattr(loop, name)(films)
    finally:
        if 'setup' in steps or 'up' in steps:
            r.down()
    data = json.loads(evidence.read_text()) if evidence.exists() else {'checks': []}
    failed = [c['step'] for c in data['checks'] if not c['ok']]
    print(json.dumps({'checks': len(data['checks']), 'failed': failed, 'skipped': [c['step'] for c in data['checks'] if c.get('skipped')]}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
