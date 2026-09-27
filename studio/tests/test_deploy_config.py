"""A release's config dir is checked with the new code, the worker's lease rule included."""
import unittest

from studio.check_config import problems
from studio.migrate_config import migrate
from studio.tests.test_migrate_config import live_shaped, store


class CheckConfigTests(unittest.TestCase):
    def setUp(self):
        api, worker = live_shaped()
        self.api, self.worker, _ = migrate(api, worker, store())
        self.worker['apilio_images'] = {'capability_roles': {'apilio_gpt_image_2_5': 'apilio_image_capability'}, 'timeout': 180}
        self.worker['lease_seconds'] = 600

    def test_a_valid_pair_passes(self):
        self.assertEqual(problems(self.api, self.worker), [])

    def test_a_timeout_the_lease_cannot_cover_is_refused(self):
        # Release 3's first attempt: apilio 600 s with a 600 s lease; the worker refused to start after the switch.
        self.worker['apilio_images']['timeout'] = 480
        found = problems(self.api, self.worker)
        self.assertEqual(len(found), 1)
        self.assertIn('lease_seconds 600 must exceed 2 × apilio_images.timeout 480 + 5', found[0])
        self.worker['lease_seconds'] = 1000
        self.assertEqual(problems(self.api, self.worker), [])

    def test_the_higgsfield_timeout_counts_for_the_lease(self):
        # the Higgsfield adapter is back; its call timeout bounds the lease like the others.
        self.worker['hf'] = {'native_path': '/tmp/example-studio/bin/hf', 'sha256': 'a' * 64, 'version': 'higgsfield 1.1.23',
                             'credential_home': '/tmp/example-studio/hf-home', 'service_uid': 501,
                             'capability_roles': {'seedance_2_5': 'hf_video_capability'}, 'timeout': 120}
        self.worker['lease_seconds'] = 240
        self.worker['apilio_images']['timeout'] = 60
        self.assertIn('must exceed 2 × hf.timeout 120 + 5', problems(self.api, self.worker)[0])

    def test_a_config_the_models_refuse_is_named(self):
        self.worker['concurrency'] = 99
        self.api['public_origin'] = 7
        found = problems(self.api, self.worker)
        self.assertTrue(found[0].startswith('api.json does not match the code'))
        self.assertTrue(found[1].startswith('worker.json does not match the code'))


class LaunchPathTests(unittest.TestCase):
    def load_launcher(self, env, binaries):
        import os
        import runpy
        from pathlib import Path
        from unittest.mock import patch
        with patch.dict(os.environ, env, clear=True), patch('shutil.which', side_effect=binaries.get):
            return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'launch.py'))

    def test_explicit_paths_take_precedence(self):
        from pathlib import Path
        launch = self.load_launcher({'MVGP_STUDIO': '/tmp/custom-studio',
            'MVGP_CLOUDFLARED': '/custom/cloudflared', 'MVGP_FFMPEG_BIN': '/custom/media'},
            {'cloudflared': '/path/cloudflared', 'ffmpeg': '/path/ffmpeg'})
        self.assertEqual(launch['STUDIO'], Path('/tmp/custom-studio'))
        self.assertEqual(launch['PY'], Path('/tmp/custom-studio/venv/bin/python'))
        self.assertEqual(launch['CLOUDFLARED'], '/custom/cloudflared')
        self.assertEqual(launch['FFMPEG_BIN'], '/custom/media')

    def test_path_binaries_are_used_without_overrides(self):
        launch = self.load_launcher({}, {'cloudflared': '/path/cloudflared', 'ffmpeg': '/media/bin/ffmpeg'})
        self.assertEqual(launch['CLOUDFLARED'], '/path/cloudflared')
        self.assertEqual(launch['FFMPEG_BIN'], '/media/bin')

    def test_existing_defaults_are_last_resort_for_empty_or_unset_overrides(self):
        from pathlib import Path
        for env in ({}, {'MVGP_STUDIO': '', 'MVGP_CLOUDFLARED': '', 'MVGP_FFMPEG_BIN': ''}):
            with self.subTest(env=env):
                launch = self.load_launcher(env, {})
                self.assertEqual(launch['STUDIO'], Path('/Users/Shared') / 'mvgp-studio')
                self.assertEqual(launch['CLOUDFLARED'], '/opt/homebrew/bin/cloudflared')
                self.assertEqual(launch['FFMPEG_BIN'], '/opt/homebrew/Cellar/ffmpeg/9.0.2/bin')


if __name__ == '__main__':
    unittest.main()
