"""An HF-shaped project folder reads into units with stable identities (production/FOLDER.md)."""
import tempfile
import unittest
from pathlib import Path

from production.folder import FolderError, project_id, read

PROMPT = '''SCENE CONTEXT
A blue cart passes a red marker at noon.

ACTIVE REFERENCES
@bluecart
@loc_lane

ACTION TIMING
0.0–5.0s — the cart rolls from frame-left to frame-right.'''


class FolderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.write('brief.md', 'project: project_abc123\n# Logline\nA cart keeps going.\n')
        self.write('script.md', '\nEXT. ROAD - DAY\nA cart passes.\n\n')
        self.write('registry.md', '## Looks\n### city\nPhotoreal noon, dry grass.\n### dream\nSoft pastel.\n\n'
                   '## @bluecart · prop\na small blue delivery cart, two wheels\n\n## @loc_lane · location\na straight country road\n')
        self.write('ASSETS/PROPS/@bluecart.md', 'A small blue delivery cart on a grey background.')
        (self.root / 'ASSETS/PROPS/@bluecart.png').write_bytes(b'\x89PNG cart')
        self.write('ASSETS/LOCATIONS/@loc_lane.md', 'A straight road seen from the south.')
        self.write('SCENE 02 - ROAD/shotlist.md', 'The cart crosses the marker.\n\n'
                   f'## 010A · 5s · 蓝车从路标左边开到右边\nlook: city\n\n{PROMPT}\n\n'
                   f'## 020A · 6s · 车继续往右开\n{PROMPT}\n')

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_the_folder_reads_into_units_with_identity_keys(self):
        units = {u.key: u for u in read(self.root)}
        self.assertEqual(set(units), {'brief', 'script', 'look:city', 'look:dream', 'asset:@bluecart', 'asset:@loc_lane',
                                      'image:@bluecart', 'image:@loc_lane', 'scene:02', 'shot:02:010A', 'shot:02:020A'})
        self.assertEqual(project_id(self.root), 'project_abc123')
        self.assertEqual(units['brief'].content['text'], '# Logline\nA cart keeps going.')
        self.assertEqual(units['asset:@bluecart'].content, {'tag': '@bluecart', 'kind': 'prop', 'descriptor': 'a small blue delivery cart, two wheels'})
        self.assertEqual(units['look:city'].content['text'], 'Photoreal noon, dry grass.')
        self.assertIsNotNone(units['image:@bluecart'].content['image_sha256'])
        self.assertIsNone(units['image:@loc_lane'].content['image_sha256'])
        shot = units['shot:02:010A'].content
        self.assertEqual((shot['label'], shot['seconds'], shot['goal'], shot['look']), ('S02-010A', 5, '蓝车从路标左边开到右边', 'city'))
        self.assertEqual(shot['prompt'], PROMPT)  # the writer's text exactly
        self.assertIsNone(units['shot:02:020A'].content['look'])
        self.assertEqual(units['scene:02'].content, {'number': '02', 'name': 'ROAD', 'text': 'The cart crosses the marker.',
                                                     'shots': ['S02-010A', 'S02-020A']})

    def test_an_edit_changes_only_its_units_digest(self):
        before = {u.key: u.digest for u in read(self.root)}
        text = (self.root / 'SCENE 02 - ROAD/shotlist.md').read_text().replace('车继续往右开', '车继续往右开，不回头')
        self.write('SCENE 02 - ROAD/shotlist.md', text)
        after = {u.key: u.digest for u in read(self.root)}
        self.assertEqual({k for k in before if before[k] != after[k]}, {'shot:02:020A'})

    def test_a_recreation_shot_names_its_source_before_the_prompt(self):
        # `source:` and `look:` in either order; a shot without one keeps its old digest.
        before = {u.key: u.digest for u in read(self.root)}
        text = (self.root / 'SCENE 02 - ROAD/shotlist.md').read_text()
        self.write('SCENE 02 - ROAD/shotlist.md', text.replace('look: city\n', 'source: obj_source01\nlook: city\n'))
        units = {u.key: u for u in read(self.root)}
        shot = units['shot:02:010A'].content
        self.assertEqual((shot['source'], shot['look'], shot['prompt']), ('obj_source01', 'city', PROMPT))
        self.assertNotIn('source', units['shot:02:020A'].content)
        self.assertEqual(units['shot:02:020A'].digest, before['shot:02:020A'])

    def test_the_writers_own_headings_stay_in_the_prompt(self):
        # Re-audit 2026-09-27: HF prompts carry `## ` headings in 3 of the first 11 projects ("## SHOT 1 — …").
        prompt = '## SHOT 1 — establishing wide\n@bluecart rolls east.\n\n## NEGATIVE PROMPT\nno text on screen'
        self.write('SCENE 02 - ROAD/shotlist.md', f'## 010A · 5s · 蓝车开过路标\n{prompt}\n\n## 020A · 6s · 车继续往右开\n{PROMPT}\n')
        units = {u.key: u for u in read(self.root)}
        self.assertEqual(units['shot:02:010A'].content['prompt'], prompt)
        self.assertEqual(units['scene:02'].content['shots'], ['S02-010A', 'S02-020A'])

    def test_every_problem_is_listed(self):
        self.write('registry.md', '## @bluecart · prop\ncart\n## @bluecart · prop\nagain\n## @x · vehicle\n?\n')
        self.write('SCENE 03 - FIELD/shotlist.md', '## 010A 5s no dots\nprompt\n## 020A · 45s · too long\nprompt\n'
                   '## 030A · 5s · named look\nlook: noir\nprompt\n')
        self.write('ASSETS/CHARACTERS/kel.md', 'no at sign')
        with self.assertRaises(FolderError) as error:
            read(self.root)
        text = ' | '.join(error.exception.problems)
        self.write('ASSETS/PROPS/@cart-.md', 'a tag the prompt cannot read')  # bug hunt 2026-09-27: @cart- reads as @cart
        with self.assertRaises(FolderError) as error:
            read(self.root)
        text = ' | '.join(error.exception.problems)
        self.assertIn('@cart-.md: the file name is the asset tag', text)
        for expected in ('@bluecart is listed twice', 'not a registry header', 'a shot header is', 'runs 45 s',
                         'names look noir', 'the file name is the asset tag', '@loc_lane is not a location'):
            self.assertIn(expected, text)


if __name__ == '__main__':
    unittest.main()
