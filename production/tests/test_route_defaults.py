"""A video route's default resolution is its first listed one (owner 2026-09-29: Higgsfield releases 1080p, then 720p)."""
import json
import unittest
from pathlib import Path

from production.compiler import route_defaults


class _Config:
    def __init__(self, sections):
        self.sections = sections

    def section(self, name):
        return self.sections[name]


class RouteDefaultsTests(unittest.TestCase):
    def test_the_released_higgsfield_route_defaults_to_1080p_and_releases_720p(self):
        runtime = json.loads((Path(__file__).resolve().parents[1] / 'config/runtime.json').read_text())
        routes = runtime['routes']['video_routes']
        profile = routes['profiles'][routes['method_routes']['mvgp-video-hf-v1']]
        self.assertEqual(profile['resolutions'][0], '1080p')
        self.assertIn('720p', profile['resolutions'])
        # every released resolution has a Higgsfield price and an output-contract size
        prices = runtime['capabilities']['hf_video_price']['credits_per_second']
        self.assertTrue(set(profile['resolutions']) <= set(prices))
        self.assertTrue(set(profile['resolutions']) <= set(profile['output_contract']['minimum_pixels_by_resolution']))

    def test_the_first_listed_resolution_is_the_default(self):
        config = _Config({'video_routes': {'method_routes': {'m': 'p', 'd': 'q'},
                                           'profiles': {'p': {'job_type': 'seedance_2_5', 'resolutions': ['1080p', '720p']},
                                                        'q': {'job_type': 'fal_seedance_2_5', 'resolutions': ['480p']}}}})
        self.assertEqual(route_defaults(config, 'm'), {'model': 'seedance_2_5', 'resolution': '1080p'})
        self.assertEqual(route_defaults(config, 'd'), {'model': 'fal_seedance_2_5', 'resolution': '480p'})
        self.assertEqual(route_defaults(config, 'missing'), {})


if __name__ == '__main__':
    unittest.main()
