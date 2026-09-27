"""The manuals ship with the code and carry a version."""
import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from production import playbook
from production.contracts import DomainError

REPO = Path(__file__).resolve().parents[2]


class PlaybookTests(unittest.TestCase):
    def test_every_packaged_manual_matches_its_recorded_hash(self):
        rows = playbook.manifest()['files']
        self.assertEqual({r['name'] for r in rows},
                         {'writer.md', 'cinedance-v4-seedance.md', 'acting-system.md', 'lira-image-prompts.md'})
        for row in rows:
            data = (playbook.ROOT / row['name']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row['sha256'], row['name'])
        writer = next(row for row in rows if row['name'] == 'writer.md')
        self.assertEqual(writer['source'], 'repo:production/playbooks/writer.md')
        self.assertEqual(REPO / writer['source'].removeprefix('repo:'), playbook.ROOT / 'writer.md')
        self.assertTrue(all(row['source'] == 'Higgsfield official skill (public)'
                            for row in rows if row['name'] != 'writer.md'))

    def test_files_serve_the_text_and_the_version_follows_the_manifest(self):
        served = playbook.files()
        self.assertTrue(all(f['text'] and len(f['sha256']) == 64 for f in served))
        self.assertIn('CINEDANCE', next(f['text'] for f in served if f['name'] == 'cinedance-v4-seedance.md').upper())
        self.assertRegex(playbook.version(), r'^pb-[0-9a-f]{12}$')
        self.assertEqual(playbook.version(), 'pb-' + hashlib.sha256((playbook.ROOT / 'manifest.json').read_bytes()).hexdigest()[:12])

    def test_a_changed_copy_is_refused_not_served(self):
        real = playbook.manifest()
        forged = json.loads(json.dumps(real))
        forged['files'][0]['sha256'] = '0' * 64
        with mock.patch.object(playbook, 'manifest', return_value=forged), self.assertRaises(DomainError) as refused:
            playbook.files()
        self.assertEqual(refused.exception.code, 'release_mismatch')

    def test_the_manuals_are_tracked_so_the_deploy_copies_them(self):
        # The deploy copies the git-tracked code dir, manuals included.
        tracked = set(subprocess.run(['git', '-C', str(REPO), 'ls-files', 'production/playbooks'], capture_output=True,
                                     text=True, check=True).stdout.split())
        if not tracked:
            self.skipTest('playbooks not committed yet')
        self.assertEqual(tracked, {f'production/playbooks/{n}' for n in
                                   ('manifest.json', 'writer.md', 'cinedance-v4-seedance.md', 'acting-system.md', 'lira-image-prompts.md')})


if __name__ == '__main__':
    unittest.main()
