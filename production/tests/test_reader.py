"""Budgeted native observations retain uncertainty; all transport is fake."""
import base64
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from production.contracts import DomainError, ObserveRequest
from production.jobs import Jobs
from production.reader import (
    ENDPOINT,
    Reader,
    ReaderResponse,
    ReaderTransportError,
    _http,
)
from production.tests import test_submissions
from production.tests.fixtures import hf_era_document


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        original = Path(cls.tmp.name) / 'original.mp4'
        slow = Path(cls.tmp.name) / 'slow.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=32x32:r=10:d=1',
                        '-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-pix_fmt','yuv420p',
                        '-c:a','aac','-shortest',str(original)],check=True)
        subprocess.run(['ffmpeg','-v','error','-i',str(original),'-vf','setpts=4*PTS','-an',
                        '-r','10','-t','4',str(slow)],check=True)
        cls.video, cls.slow = original.read_bytes(), slow.read_bytes()

    def setUp(self):
        self.s = test_submissions.SubmissionTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.f, self.store, self.pid = self.s.f,self.s.store,self.s.pid
        self.routes = hf_era_document('review_routes')
        self.profile_id = self.routes['role_routes']['observer']
        self.profile = self.routes['profiles'][self.profile_id]
        self.s.policy['operations']['observe'] = {'video':copy.deepcopy(self.s.cost)}
        self.publish()
        self.source = self.f.media.put(self.pid,[self.video],'video/mp4','upload_service',logical_path='source.mp4')
        self.worker = self.f.auth.authenticate(self.f.auth.provision_token('observer','worker',[self.pid],300))
        self.jobs = Jobs(self.store,self.f.auth,self.s.service,self.f.media,lease_seconds=300)
        self.calls = []
        self.output = {'observations':[{'input_id':'original','timestamp_seconds':0.5,'modality':'visual',
                       'fact':'Red fills the image.','interpretation':None}],
                       'answers':[{'question_index':0,'verdict':'supported','evidence_indices':[0],
                                   'explanation':'The image is red.'}], 'uncertainty':['Exact consumed frames are unknown.']}
        self.response = {'modelVersion':'gemini-3.8-flash','candidates':[{'finishReason':'STOP',
            'content':{'parts':[{'text':json.dumps(self.output)}]}}],
            'usageMetadata':{'promptTokensDetails':[{'modality':'VIDEO','tokenCount':100}],
                             'promptTokenCount':120,'candidatesTokenCount':80}}
        def transport(endpoint,payload,headers,timeout,max_bytes):
            self.calls.append((endpoint,payload,headers))
            return ReaderResponse(200,json.dumps(self.response).encode())
        self.transport = transport
        self.reader = Reader(self.jobs,self.f.flow.config,transport=transport)

    def publish(self):
        self.f.config.set('review_routes', self.routes)
        self.f.config.set('execution_policy', self.s.policy)
        self.s.c.activate()

    def dispatch(self, *, scale=1.0, offset=0.0, key='observe'):
        job = self.s.service.observe(self.s.actor,self.pid,ObserveRequest(idempotency_key=key,expected_revision=1,
            media_id=self.source['object_id'],reader='video',questions=['Is the image red?'],time_scale=scale,source_offset_seconds=offset))
        claim = self.jobs.claim(self.worker,self.pid,job['object_id'])
        return self.jobs.begin_dispatch(self.worker,self.pid,self.f.ref(claim['job']),claim['fence'])

    def read(self, dispatch=None, **kwargs):
        d = dispatch or self.dispatch()
        return self.reader.observe(self.worker,self.pid,self.f.ref(d['job']),d['fence'],**kwargs)

    def test_revoked_maker_after_dispatch_blocks_supplier_call(self):
        # was the employee-removal case; the rule stays for any revoked maker credential.
        token = self.f.auth.provision_token('maker_to_revoke', 'agent', [self.pid], 300)
        self.s.actor = self.f.auth.authenticate(token)
        dispatched = self.dispatch()
        self.f.auth.revoke(self.s.actor.credential_id)
        with self.assertRaises(DomainError): self.read(dispatched)
        self.assertEqual(self.calls, [])

    def test_observer_reserves_and_revalidates_native_account(self):
        self.s.policy['operations']['observe']['video']['budget_key'] = 'apilio_main'
        self.publish()
        self.store.set_budget(self.pid, 20, 'credit', budget_key='apilio_main')
        self.reader = Reader(self.jobs, self.f.flow.config, transport=self.transport)
        d = self.dispatch()
        self.assertEqual(self.read(d)['status'], 'succeeded')
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)
        self.assertEqual(self.store.budget(self.pid, budget_key='apilio_main')['reserved'], 10)
        self.store.set_budget(self.pid, 20, 'credit', budget_key='apilio_other')
        with self.store.transaction() as db:
            db.execute('UPDATE reservations SET budget_key=? WHERE reservation_id=?',
                       ('apilio_other', d['job']['body']['reservation_id']))
        with self.assertRaises(DomainError) as error:
            self.read(d)
        self.assertEqual(error.exception.code, 'budget_exceeded')
        self.assertEqual(len(self.calls), 1)

    def test_observer_missing_native_envelope_does_not_use_legacy(self):
        self.s.policy['operations']['observe']['video']['budget_key'] = 'apilio_main'
        self.publish()
        with self.assertRaises(DomainError):
            self.dispatch()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)

    def test_real_original_bytes_model_usage_and_no_approval(self):
        with patch.dict('os.environ',{'APILIO_AI_KEY':'never-read'}):
            body = self.read()
        self.assertEqual(body['status'],'succeeded')
        self.assertEqual(body['source'],self.f.ref(self.source).model_dump())
        self.assertTrue(body['consumption']['video_evidence_available'])
        self.assertFalse(body['consumption']['audio_evidence_available'])
        self.assertEqual(body['consumption']['sampling_coverage'],'unknown')
        self.assertNotIn('accepted',body)
        self.assertNotIn('never-read',str(self.calls))
        parts = self.calls[0][1]['contents'][0]['parts']
        self.assertEqual(base64.b64decode(next(p['inlineData']['data'] for p in parts if 'inlineData' in p)),self.video)
        self.assertEqual(self.store.list_objects(self.pid,kind='observation'),[])
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_single_complete_json_fence_preserves_raw_and_audio_uncertainty(self):
        # Shape observed in route-probe-0918/gemini-audio.json; only the wrapper
        # is reusable here, not that probe's unrelated output schema.
        self.reader = Reader(self.jobs, self.f.flow.config, transport=self.transport)
        text = '```json\n' + json.dumps(self.output) + '\n```'
        self.response['candidates'][0]['content']['parts'][0]['text'] = text
        dispatch = self.dispatch()
        body = self.read(dispatch)
        self.assertEqual(body['status'], 'succeeded')
        self.assertEqual(body['raw_response'], text)
        self.assertEqual(body['observations'][0]['fact'], self.output['observations'][0]['fact'])
        self.assertTrue(body['consumption']['video_evidence_available'])
        self.assertFalse(body['consumption']['audio_evidence_available'])
        self.assertEqual(len(self.calls), 1)

    def test_prose_ambiguous_fences_and_invalid_inner_objects_fail_without_retry(self):
        d = self.dispatch()
        valid = json.dumps(self.output)
        fence = '```json\n' + valid + '\n```'
        texts = ['Analysis\n' + valid, '{invalid', 'Analysis\n' + fence,
                 fence + '\nExplanation', fence + '\n' + fence,
                 '```python\n' + valid + '\n```', '```\n' + valid + '\n```',
                 '```json\n' + valid, '```json\n{invalid\n```',
                 '```json\n' + valid[:-1] + ',"uncertainty":[]}\n```',
                 '```json\n' + valid + '{}\n```']
        for stamp in (-1,100,float('nan')):
            output = copy.deepcopy(self.output)
            output['observations'][0]['timestamp_seconds'] = stamp
            texts.append(json.dumps(output))
            texts.append('```json\n' + json.dumps(output) + '\n```')
        for corrupt in ('extra', 'evidence', 'question'):
            output = copy.deepcopy(self.output)
            if corrupt == 'extra':
                output['authoritative_pass'] = True
            elif corrupt == 'evidence':
                output['answers'][0]['evidence_indices'] = [999]
            else:
                output['answers'][0]['question_index'] = 999
            texts.append('```json\n' + json.dumps(output) + '\n```')
        for text in texts:
            with self.subTest(text=text[:20]):
                self.response['candidates'][0]['content']['parts'][0]['text'] = text
                body = self.read(d)
                self.assertEqual(body['status'],'failed')
                self.assertIn(text,body['raw_response'])
        self.assertEqual(len(self.calls),len(texts))

    def test_missing_video_usage_or_model_never_certifies_delivery(self):
        d = self.dispatch()
        for field in ('modelVersion','usageMetadata'):
            response = self.response.pop(field)
            body = self.read(d)
            self.assertEqual(body['status'],'failed')
            self.assertFalse(body['consumption']['video_evidence_available'])
            self.response[field] = response
        self.response['modelVersion'] = 'different-model'
        self.assertEqual(self.read(d)['status'],'failed')

    def test_audio_evidence_requires_usage_and_original_audio(self):
        self.response['usageMetadata']['promptTokensDetails'].append({'modality':'AUDIO','tokenCount':20})
        body = self.read()
        self.assertTrue(body['consumption']['audio_evidence_available'])

    def audible_response(self):
        self.output['observations'].append({'input_id':'original','timestamp_seconds':0.4,
            'modality':'audible','fact':'A steady tone is audible.','interpretation':None})
        self.response['candidates'][0]['content']['parts'][0]['text'] = json.dumps(self.output)

    def test_calibrated_embedded_audio_is_evidence_without_inventing_usage(self):
        self.audible_response()
        body = self.read()
        self.assertEqual(body['status'], 'succeeded')
        self.assertTrue(body['consumption']['audio_evidence_available'])
        self.assertEqual(body['consumption']['audio_evidence_basis'], 'calibrated_embedded_audio_observations')
        self.assertFalse(body['consumption']['audio_usage_reported'])
        self.assertNotIn('AUDIO', str(body['usage']))
        self.assertTrue(any('calibrated' in text and 'unverified' in text for text in body['uncertainty']))
        self.assertEqual(len(self.calls), 1)

    def test_uncalibrated_or_unsupported_route_cannot_use_audio_claims_as_delivery(self):
        self.audible_response()
        self.store.set_budget(self.pid, 100, 'credit')
        for missing in ('calibration', 'evidence_id', 'modality', 'encoding'):
            with self.subTest(missing=missing):
                profile = copy.deepcopy(self.profile)
                if missing == 'calibration':
                    profile['observed_settings']['speech_phrase_correct_in_sound_bearing_mp4'] = False
                elif missing == 'evidence_id':
                    profile['evidence_ids'].remove('gemini-audio')
                elif missing == 'modality':
                    profile['allowed_input_modalities'].remove('audio_in_video')
                else:
                    profile['supported_media_encodings']['audio_in_video'] = []
                self.routes['profiles'][self.profile_id] = profile
                self.publish()
                self.reader = Reader(self.jobs,self.f.flow.config,transport=self.transport)
                self.source = self.f.media.put(self.pid,[self.video],'video/mp4','upload_service',logical_path=missing+'.mp4')
                body = self.read(self.dispatch(key='audio-'+missing))
                self.assertEqual(body['status'], 'succeeded')
                self.assertFalse(body['consumption']['audio_evidence_available'])

    def test_audio_usage_does_not_make_a_silent_original_audible(self):
        self.source = self.f.media.put(self.pid,[self.slow],'video/mp4','upload_service',logical_path='silent.mp4')
        self.response['usageMetadata']['promptTokensDetails'].append({'modality':'AUDIO','tokenCount':20})
        body = self.read()
        self.assertFalse(body['consumption']['audio_evidence_available'])
        self.assertEqual(body['consumption']['audio_evidence_basis'], 'none')

    def test_audible_claim_on_silent_source_is_rejected(self):
        self.source = self.f.media.put(self.pid,[self.slow],'video/mp4','upload_service',logical_path='silent.mp4')
        self.audible_response()
        body = self.read()
        self.assertEqual(body['status'], 'failed')
        self.assertFalse(body['consumption']['audio_evidence_available'])

    def test_retiming_preserves_original_audio_and_server_time_mapping(self):
        derivative = self.f.media.put(self.pid,[self.slow],'video/mp4','worker_service',logical_path='slow.mp4',
                                      derivative_of=self.f.ref(self.source))
        self.output['observations'][0].update(input_id='visual_derivative',timestamp_seconds=2.0)
        self.response['candidates'][0]['content']['parts'][0]['text'] = json.dumps(self.output)
        body = self.read(self.dispatch(scale=4.0,offset=690.0),visual_derivative=self.f.ref(derivative))
        self.assertEqual(body['status'],'succeeded')
        self.assertEqual(body['observations'][0]['source_seconds'],690.5)
        self.assertEqual([x['time_scale'] for x in body['inputs']],[1.0,4.0])
        self.assertEqual(len([p for p in self.calls[0][1]['contents'][0]['parts'] if 'inlineData' in p]),2)
        self.assertEqual(body['consumption']['sampling_coverage'],'unknown')

    def test_retiming_without_valid_derivative_rejects_before_transport(self):
        d = self.dispatch(scale=4.0)
        for derivative in (None,self.f.ref(self.source)):
            with self.assertRaises(DomainError):
                self.read(d,visual_derivative=derivative)
        self.assertEqual(self.calls,[])

    def test_unbudgeted_expired_fence_and_author_cannot_read(self):
        d = self.dispatch()
        with self.assertRaises(DomainError):
            self.reader.observe(self.s.actor,self.pid,self.f.ref(d['job']),d['fence'])
        with self.assertRaises(DomainError):
            self.reader.observe(self.worker,self.pid,self.f.ref(d['job']),d['fence']+1)
        self.store.settle(self.pid,d['job']['body']['reservation_id'],0)
        with self.assertRaises(DomainError):
            self.read(d)
        self.assertEqual(self.calls,[])

    def test_response_limit_redirect_and_transport_ambiguity_no_retry(self):
        d = self.dispatch()
        for response in (ReaderResponse(302,b'go elsewhere'),ReaderResponse(200,b'x'*1048577)):
            self.reader = Reader(self.jobs,self.f.flow.config,transport=lambda *args, response=response:response)
            self.assertEqual(self.read(d)['status'],'failed')
        count = []
        def fail(*args):
            count.append(1)
            raise TimeoutError('ambiguous transport')
        self.reader = Reader(self.jobs,self.f.flow.config,transport=fail)
        self.assertEqual(self.read(d)['status'],'unknown')
        self.assertEqual(len(count),1)

    def test_live_pricing_missing_blocks_before_credential_lookup(self):
        self.s.policy['operations']['observe']['video'].update(mode='live', budget_key='apilio_main')
        self.store.set_budget(self.pid, 20, 'credit', budget_key='apilio_main')
        self.s.policy['operations']['observe']['video']['live_controls'] = {
            'pricing_verified':True,'isolation_verified':True,'account_limits_verified':True}
        self.profile['enablement'].update(live_enabled=True,isolation_verified=True)
        self.publish()
        self.s.service.live_enabled = True
        d = self.dispatch()
        live = Reader(self.jobs,self.f.flow.config,credential_provider=lambda _:self.fail('Must not read credentials'))
        with self.assertRaises(DomainError):
            live.observe(self.worker,self.pid,self.f.ref(d['job']),d['fence'])
        self.assertEqual(self.calls,[])

    def test_standalone_modalities_and_incompatible_route_reject(self):
        d = self.dispatch()
        frozen = copy.deepcopy(d['intent']['body'])
        for modality in ('image','audio'):
            frozen['request']['reader'] = modality
            with self.assertRaises(DomainError):
                self.reader._settings(frozen,self.profile)
        frozen['request']['reader'] = 'video'
        for field,value in [('endpoint','https://evil.example'),('model','invented-model')]:
            profile = copy.deepcopy(self.profile)
            profile[field] = value
            with self.assertRaises(DomainError):
                self.reader._settings(frozen,profile)
        self.assertEqual(self.calls,[])

    def test_ignored_fps_and_token_growth_never_become_sampling_guarantee(self):
        d = self.dispatch()
        for count in (100,400):
            self.response['usageMetadata']['promptTokensDetails'][0]['tokenCount'] = count
            body = self.read(d)
            self.assertIsNone(body['consumption']['effective_fps'])
            self.assertEqual(body['consumption']['sampling_coverage'],'unknown')

    def test_model_cannot_invent_review_authority_or_unbound_question_evidence(self):
        d = self.dispatch()
        for alteration in ('approval','empty-evidence','wrong-index'):
            output = copy.deepcopy(self.output)
            if alteration == 'approval':
                output['approved'] = True
            elif alteration == 'empty-evidence':
                output['answers'][0]['evidence_indices'] = []
            else:
                output['answers'][0]['evidence_indices'] = [9]
            self.response['candidates'][0]['content']['parts'][0]['text'] = json.dumps(output)
            self.assertEqual(self.read(d)['status'],'failed')

    def test_native_http_uses_fixed_process_with_exact_wire_and_private_headers(self):
        from production import review_http
        from production.contracts import canonical_json
        payload = {'contents': [{'text': '原速声音'}]}
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer private-key'}
        with patch('production.reader.review_http.request', return_value=review_http.HTTPResponse(200, b'{}')) as send:
            result = _http(ENDPOINT, payload, headers, 120, 1024)
        self.assertEqual(result, ReaderResponse(200, b'{}'))
        endpoint, wire, actual_headers, timeout, bound = send.call_args.args
        self.assertEqual((endpoint, wire, bound), (ENDPOINT, canonical_json(payload).encode(), 1024))
        self.assertEqual(actual_headers, {**headers, 'Accept-Encoding': 'identity'})
        self.assertTrue(0 < timeout <= 120)
        self.assertNotIn('Accept-Encoding', headers)

    def test_native_helper_unknown_holds_and_fatal_propagates_without_retry(self):
        from production import review_http
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer private-key'}
        with patch('production.reader.review_http.request', side_effect=review_http.UnknownOutcome()) as send, self.assertRaises(DomainError) as error:
            _http(ENDPOINT, {}, headers, 1, 1024)
        self.assertEqual(error.exception.code, 'unknown_outcome')
        send.assert_called_once()
        with patch('production.reader.review_http.request', side_effect=review_http.FatalWorkerError()), self.assertRaises(review_http.FatalWorkerError):
            _http(ENDPOINT, {}, headers, 1, 1024)
        self.reader.transport = lambda *args: (_ for _ in ()).throw(review_http.FatalWorkerError())
        with self.assertRaises(review_http.FatalWorkerError):
            self.read()
        self.assertEqual(self.store.budget(self.pid)['reserved'], 10)

    def test_native_failure_reason_survives_without_private_exception_text(self):
        from production import review_http
        with patch('production.reader.review_http.request', side_effect=review_http.UnknownOutcome('read_timeout')) as send, self.assertRaises(DomainError) as error:
            _http(ENDPOINT, {}, {'Authorization': 'Bearer private-key'}, 120, 1024)
        self.assertEqual(error.exception.diagnostic, 'read_timeout')
        self.assertNotIn('private-key', str(error.exception))
        send.assert_called_once()

    def test_observation_keeps_only_allowlisted_transport_diagnostic(self):
        dispatched = self.dispatch()
        for supplied, expected in [('read_timeout', 'read_timeout'), ('Bearer private-key', 'unavailable')]:
            with self.subTest(supplied=supplied):
                error = ReaderTransportError(supplied)
                with patch.object(self.reader, 'transport', side_effect=error) as send:
                    result = self.read(dispatched)
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['failure'], 'transport_outcome_unknown')
                self.assertEqual(result['diagnostic'], expected)
                self.assertEqual(result['reservation_action'], 'hold')
                self.assertNotIn('private', json.dumps(result))
                send.assert_called_once()

    def test_actual_base64_wire_bound_rejects_before_fake_or_native_io(self):
        from production import review_http
        d = self.dispatch()
        original_input = self.reader._input
        def large_input(*args):
            info, _ = original_input(*args)
            raw = b'x' * (25 * 1024 * 1024)
            return {**info, 'bytes': len(raw)}, raw
        with patch.object(self.reader, '_input', side_effect=large_input), self.assertRaises(DomainError) as error:
            self.read(d)
        self.assertEqual(error.exception.code, 'insufficient_context')
        self.assertEqual(self.calls, [])
        with patch('production.reader.review_http.request', side_effect=AssertionError('No IO')), self.assertRaises(DomainError):
            _http(ENDPOINT, b'x' * (review_http.MAX_REQUEST_BYTES + 1), {}, 1, 1024)

    def test_preparation_elapsed_time_and_current_lease_shorten_dispatch(self):
        from production.contracts import canonical_json
        d = self.dispatch()
        authority = self.reader._authority
        calls = []
        def current(*args):
            intent, profile, remaining, attempt = authority(*args)
            calls.append(True)
            return intent, profile, 12 if len(calls) > 1 else remaining, attempt
        with patch.object(self.reader, '_authority', side_effect=current),\
                patch('production.reader.time.monotonic', side_effect=[10, 110]),\
                patch.object(self.reader, 'transport', wraps=self.reader.transport) as send:
            body = self.read(d)
        self.assertEqual(body['status'], 'succeeded')
        self.assertEqual(len(calls), 2)
        self.assertEqual(send.call_args.args[3], 7)
        self.assertEqual(body['request_sha256'], hashlib.sha256(canonical_json(self.calls[0][1]).encode()).hexdigest())

    def test_serialization_exhaustion_sends_nothing(self):
        with patch('production.reader.time.monotonic', side_effect=[0, 2]),\
                patch('production.reader.review_http.request', side_effect=AssertionError('No IO')), self.assertRaises(DomainError):
            _http(ENDPOINT, {}, {}, 1, 1024)
        d = self.dispatch()
        with patch('production.reader.time.monotonic', side_effect=[0, 121]):
            body = self.read(d)
        self.assertEqual(body['status'], 'unknown')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 10)
