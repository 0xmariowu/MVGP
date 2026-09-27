"""LOCAL delivered-pixel checks; no supplier or native-resolution claims."""
import copy
import math
import unittest

from production.contracts import DomainError, canonical_json
from production.output_contract import check_result, validate_policy, validate_request


def image_policy():
    return {'schema_version':1,'authority':'LOCAL','modality':'image','minimum_edge':'long',
            'minimum_pixels_by_resolution':{'1k':1024,'2k':2048,'4k':4096},
            'aspect_ratio_relative_tolerance':0.01,'duration_tolerance_seconds':None,
            'require_requested_audio':False}


def video_policy():
    return {'schema_version':1,'authority':'LOCAL','modality':'video','minimum_edge':'short',
            'minimum_pixels_by_resolution':{'720p':720,'1080p':1080},
            'aspect_ratio_relative_tolerance':0.01,'duration_tolerance_seconds':0.25,
            'require_requested_audio':True}


def video_probe(**changes):
    return {'width':1920,'height':1080,'display_width':1920.0,'display_height':1080.0,
            'display_geometry_status':'verified','video_duration':3.0,
            'video_duration_source':'stream-duration','duration':3.0,'container_duration':3.0,
            'has_video':True,'has_audio':True,**changes}


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        self.image={'resolution':'2k','aspect_ratio':'3:2'}
        self.video={'resolution':'1080p','aspect_ratio':'16:9','duration':3,'generate_audio':True}

    def test_actual_image_example_passes_without_native_resolution_claim(self):
        result=check_result(self.image,{'width':2528,'height':1696},'image/png',image_policy())
        self.assertTrue(result['passed'])
        self.assertEqual(result['reasons'],[])
        self.assertFalse(result['native_resolution_verified'])
        self.assertEqual(result['observed']['width'],2528)

    def test_small_generated_image_cannot_pass_large_label(self):
        result=check_result(self.image,{'width':12,'height':8},'image/png',image_policy())
        self.assertFalse(result['passed'])
        self.assertIn('minimum_edge_mismatch',[r['code'] for r in result['reasons']])

    def test_policy_is_strict_and_route_mapping_coverage_is_explicit(self):
        self.assertEqual(validate_policy(image_policy(),modality='image',resolutions=['2k']),image_policy())
        self.assertEqual(validate_policy(video_policy(),modality='video',resolutions=['1080p']),video_policy())
        changes=[{'schema_version':True},{'authority':'HF'},{'minimum_edge':'short'},
                 {'minimum_pixels_by_resolution':{}},{'minimum_pixels_by_resolution':{'2k':True}},
                 {'minimum_pixels_by_resolution':{'2k':2048.0}},{'minimum_pixels_by_resolution':{'2k':0}},
                 {'minimum_pixels_by_resolution':{'2k':32769}},{'minimum_pixels_by_resolution':{'1080p':1080}},
                 {'aspect_ratio_relative_tolerance':math.nan},{'aspect_ratio_relative_tolerance':math.inf},
                 {'aspect_ratio_relative_tolerance':-0.01},{'aspect_ratio_relative_tolerance':True},
                 {'aspect_ratio_relative_tolerance':0.11},{'duration_tolerance_seconds':0.25},
                 {'require_requested_audio':True},{'fake_bypass':True}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(DomainError) as error:
                validate_policy({**image_policy(),**change})
            self.assertEqual(error.exception.code,'unsupported_route')
        for policy in (None,{},[],{'schema_version':1}, {k:v for k,v in video_policy().items() if k!='duration_tolerance_seconds'}):
            with self.subTest(policy=policy), self.assertRaises(DomainError):
                validate_policy(policy)
        for kwargs in ({'modality':'video'},{'resolutions':['8k']},{'resolutions':'2k'},{'resolutions':[]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DomainError):
                validate_policy(image_policy(),**kwargs)
        for change in ({'duration_tolerance_seconds':None},{'duration_tolerance_seconds':math.inf},
                       {'duration_tolerance_seconds':-1},{'require_requested_audio':False},{'minimum_edge':'long'}):
            with self.subTest(change=change), self.assertRaises(DomainError):
                validate_policy({**video_policy(),**change})

    def test_a_480p_draft_take_is_measured_against_480_not_1080(self):
        policy={**video_policy(),'minimum_pixels_by_resolution':{'480p':480,'1080p':1080}}
        self.assertEqual(validate_policy(policy,modality='video',resolutions=['480p']),policy)
        probe={'display_width':854,'display_height':480,'display_geometry_status':'verified','has_video':True,
               'video_duration':3,'video_duration_source':'stream-duration','has_audio':True}
        self.assertTrue(check_result({**self.video,'resolution':'480p'},probe,'video/mp4',policy)['passed'])
        late=check_result(self.video,probe,'video/mp4',policy)
        self.assertIn('minimum_edge_mismatch',[r['code'] for r in late['reasons']])

    def test_request_needs_explicit_frozen_audio_and_valid_measurements(self):
        for change in ({'resolution':'4k'},{'aspect_ratio':'auto'},{'aspect_ratio':'1:0'},
                       {'aspect_ratio':'1000:1'},{'duration':math.nan},{'duration':math.inf},
                       {'duration':0},{'duration':True},{'duration':'3'},{'generate_audio':'true'},
                       {'generate_audio':1},{'generate_audio':None}):
            with self.subTest(change=change), self.assertRaises(DomainError) as error:
                validate_request({**self.video,**change},video_policy())
            self.assertEqual(error.exception.code,'invalid_input')
        for key in ('resolution','aspect_ratio','duration','generate_audio'):
            with self.subTest(key=key), self.assertRaises(DomainError):
                validate_request({k:v for k,v in self.video.items() if k!=key},video_policy())
        self.assertFalse(validate_request({**self.video,'generate_audio':False},video_policy())['generate_audio'])

    def test_long_edge_resolution_thresholds_and_exact_boundary(self):
        for resolution, minimum in image_policy()['minimum_pixels_by_resolution'].items():
            request={'resolution':resolution,'aspect_ratio':'1:1'}
            self.assertTrue(check_result(request,{'width':minimum,'height':minimum},'image/png',image_policy())['passed'])
            self.assertFalse(check_result(request,{'width':minimum-1,'height':minimum-1},'image/png',image_policy())['passed'])
        changed={**image_policy(),'minimum_pixels_by_resolution':{'2k':2500}}
        self.assertFalse(check_result({'resolution':'2k','aspect_ratio':'1:1'},{'width':2499,'height':2499},'image/png',changed)['passed'])
        self.assertTrue(check_result({'resolution':'2k','aspect_ratio':'1:1'},{'width':2500,'height':2500},'image/png',changed)['passed'])

    def test_relative_ratio_inclusive_boundaries_no_hidden_epsilon(self):
        request={'resolution':'1k','aspect_ratio':'1:1'}
        for width in (1980,2020):
            self.assertTrue(check_result(request,{'width':width,'height':2000},'image/png',image_policy())['passed'])
        for width in (1979,2021):
            result=check_result(request,{'width':width,'height':2000},'image/png',image_policy())
            self.assertFalse(result['passed'])
            self.assertIn('aspect_ratio_mismatch',[r['code'] for r in result['reasons']])
        exact={**image_policy(),'aspect_ratio_relative_tolerance':0.0}
        self.assertFalse(check_result(request,{'width':2001,'height':2000},'image/png',exact)['passed'])

    def test_displayed_short_edge_resolution_floor(self):
        for resolution, minimum in video_policy()['minimum_pixels_by_resolution'].items():
            request={**self.video,'resolution':resolution,'aspect_ratio':'1:1'}
            self.assertTrue(check_result(request,video_probe(display_width=minimum,display_height=minimum),'video/mp4',video_policy())['passed'])
            result=check_result(request,video_probe(display_width=minimum-1,display_height=minimum-1),'video/mp4',video_policy())
            self.assertFalse(result['passed'])
        ultrawide={**self.video,'aspect_ratio':'1920:824'}
        result=check_result(ultrawide,video_probe(display_width=1920,display_height=824),'video/mp4',video_policy())
        self.assertEqual([r['code'] for r in result['reasons']],['minimum_edge_mismatch'])

    def test_duration_exact_bounds_and_long_audio_cannot_cover_short_video(self):
        for duration in (2.75,3.25):
            self.assertTrue(check_result(self.video,video_probe(video_duration=duration),'video/mp4',video_policy())['passed'])
        for duration in (2.749999,3.250001):
            result=check_result(self.video,video_probe(video_duration=duration),'video/mp4',video_policy())
            self.assertIn('duration_mismatch',[r['code'] for r in result['reasons']])
        result=check_result(self.video,video_probe(video_duration=0.5,duration=3,container_duration=3),'video/mp4',video_policy())
        self.assertFalse(result['passed'])
        self.assertEqual(result['observed']['video_duration'],0.5)
        missing=video_probe(); missing.pop('video_duration')
        self.assertIn('missing_video_duration',[r['code'] for r in check_result(self.video,missing,'video/mp4',video_policy())['reasons']])
        self.assertFalse(check_result(self.video,video_probe(video_duration_source='container'),'video/mp4',video_policy())['passed'])

    def test_measured_anamorphic_and_rotated_display_used_instead_of_coded_pixels(self):
        anamorphic=video_probe(width=1440,height=1080,display_width=1920,display_height=1080,
                               sample_aspect_ratio='4:3',rotation_degrees=0)
        self.assertTrue(check_result(self.video,anamorphic,'video/mp4',video_policy())['passed'])
        portrait={**self.video,'aspect_ratio':'9:16'}
        rotated=video_probe(width=1920,height=1080,display_width=1080,display_height=1920,
                            sample_aspect_ratio='1:1',rotation_degrees=90)
        self.assertTrue(check_result(portrait,rotated,'video/quicktime',video_policy())['passed'])
        self.assertFalse(check_result(self.video,rotated,'video/mp4',video_policy())['passed'])
        self.assertFalse(check_result(self.video,video_probe(display_geometry_status='unknown'),'video/mp4',video_policy())['passed'])
        no_display=video_probe(); no_display.pop('display_width'); no_display.pop('display_height')
        self.assertFalse(check_result(self.video,no_display,'video/mp4',video_policy())['passed'])

    def test_audio_presence_only_does_not_claim_audibility_or_correctness(self):
        self.assertFalse(check_result(self.video,video_probe(has_audio=False),'video/mp4',video_policy())['passed'])
        silent_request={**self.video,'generate_audio':False}
        self.assertTrue(check_result(silent_request,video_probe(has_audio=False),'video/mp4',video_policy())['passed'])
        self.assertTrue(check_result(silent_request,video_probe(has_audio=True),'video/mp4',video_policy())['passed'])
        self.assertFalse(check_result(silent_request,video_probe(has_audio=None),'video/mp4',video_policy())['passed'])
        result=check_result(self.video,video_probe(),'video/mp4',video_policy())
        self.assertTrue(result['passed'])
        self.assertNotIn('audible',canonical_json(result))
        self.assertFalse(result['native_resolution_verified'])

    def test_wrong_modality_and_bad_measurements_fail_closed_without_nan_results(self):
        for mime in ('image/png','audio/wav','video/unknown','application/json'):
            with self.subTest(mime=mime):
                self.assertFalse(check_result(self.video,video_probe(),mime,video_policy())['passed'])
        for key in ('display_width','display_height','video_duration','has_video','has_audio'):
            for invalid in (None,True if key not in ('has_video','has_audio') else 'yes',0,-1,math.nan,math.inf,10**1000,'1080'):
                with self.subTest(key=key,invalid=str(invalid)[:20]):
                    result=check_result(self.video,video_probe(**{key:invalid}),'video/mp4',video_policy())
                    self.assertFalse(result['passed'])
                    canonical_json(result)
        for probe in (None,[],{}, {'width':2048.0,'height':2048}, {'width':True,'height':2048}):
            self.assertFalse(check_result({'resolution':'2k','aspect_ratio':'1:1'},probe,'image/png',image_policy())['passed'])

    def test_subpixel_malformed_geometry_cannot_overflow_derived_ratio(self):
        for field in ('display_width','display_height'):
            result=check_result(self.video,video_probe(**{field:5e-324}),'video/mp4',video_policy())
            self.assertFalse(result['passed'])
            canonical_json(result)

    def test_inputs_are_not_mutated_and_policy_hash_tracks_released_thresholds(self):
        params,probe,policy=copy.deepcopy(self.video),video_probe(),video_policy()
        before=copy.deepcopy((params,probe,policy))
        result=check_result(params,probe,'video/mp4',policy)
        self.assertEqual((params,probe,policy),before)
        changed={**policy,'duration_tolerance_seconds':0.2}
        self.assertNotEqual(result['policy_hash'],check_result(params,probe,'video/mp4',changed)['policy_hash'])
        self.assertNotIn('fake',result)


if __name__ == '__main__': unittest.main()
