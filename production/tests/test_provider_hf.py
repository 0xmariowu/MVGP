"""No paid calls: fake CLI transport checks exact argv and fail-closed outcomes."""
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from production import review_http
from production.contracts import DomainError
from production.provider_hf import (
    CommandResult,
    ExecutablePin,
    HFProvider,
    NativeOutputLimit,
    NativeTimeout,
    ResolvedReference,
    _native,
)


class HFProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root/'service'
        self.home.mkdir(mode=0o700)
        self.binary = self.root/'native-higgsfield'
        self.binary.write_bytes(b'\xcf\xfa\xed\xfe synthetic native fixture')
        self.binary.chmod(0o700)
        self.pin = ExecutablePin(self.binary,hashlib.sha256(self.binary.read_bytes()).hexdigest(),'higgsfield 1.1.23 synthetic fixture')
        self.caps = {
            'nano_banana_pro':{'job_type':'nano_banana_pro','type':'image','params':[
                {'name':'prompt','type':'string','required':True}, {'name':'aspect_ratio','type':'string','enum':['16:9','1:1']},
                {'name':'resolution','type':'string','enum':['1k','2k','4k']},{'name':'image_references','type':'array'}]},
            'seedance_2_5':{'job_type':'seedance_2_5','type':'video','params':[
                {'name':'prompt','type':'string','required':True}, {'name':'duration','type':'integer'},
                {'name':'aspect_ratio','type':'string','enum':['16:9']},{'name':'resolution','type':'string','enum':['720p','1080p']},
                {'name':'mode','type':'string','enum':['t2v','omni_reference','video_edit','video_extension']},
                *[{'name':name,'type':'array'} for name in ['image_references','video_references','audio_references']]]}}
        self.calls = []
        self.response = None
        def transport(argv,env,timeout,max_bytes):
            self.calls.append((argv,env))
            if argv[1:] == ['--version']:
                return CommandResult(0,self.pin.version.encode(),b'')
            if argv[1:3] == ['model','get']:
                return CommandResult(0,json.dumps(self.caps[argv[3]]).encode(),b'')
            if isinstance(self.response,Exception):
                raise self.response
            return self.response or CommandResult(0,json.dumps(self.job()).encode(),b'')
        self.transport = transport
        self.provider = self.make()

    def make(self,**kw):
        return HFProvider(self.caps,self.pin,service_home=self.home,service_uid=os.getuid(),
                          media_root=self.root,transport=self.transport,**kw)

    def request(self,video=False):
        return {'job_type':'seedance_2_5' if video else 'nano_banana_pro', 'params':{
            'prompt':'A rider moves forward; $(touch /tmp/never-run)', 'resolution':'1080p' if video else '2k',
            'aspect_ratio':'16:9', **({'duration':3,'mode':'omni_reference'} if video else {})},'references':[]}

    def intent(self,request):
        return {'operation':'submit','request':request,'cost':{'mode':'fake'}}

    def job(self,request=None):
        request = request or self.request()
        return {'id':'remote-job-1','job_type':request['job_type'],'status':'completed',
                'params':request['params'],'result_url':'https://cdn.example/result.mp4?token=secret',
                'min_result_url':None,'created_at':'2026-09-18',
                'message':'download https://cdn.example/result.mp4?token=secret after processing'}

    def reference(self,request,media_type='image/png'):
        path = self.root/f'reference{len(request["references"])}.bin'
        path.write_bytes(b'verified fixture media')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ref = {'object_id':f'media_{len(request["references"])}','revision':1,'digest':'a'*64}
        request['references'].append({'object_ref':ref,'sha256':digest,'media_type':media_type,'role':'identity'})
        return ResolvedReference(ref,path,digest,media_type)

    def test_exact_argv_single_call_and_environment_isolation(self):
        request = self.request()
        ref = self.reference(request)
        self.response = CommandResult(0,json.dumps(self.job(request)).encode(),b'')
        with patch.dict(os.environ,{'OPENAI_API_KEY':'secret','NODE_OPTIONS':'evil','HTTPS_PROXY':'evil','HOME':'/attacker'}):
            result = self.provider.submit(self.intent(request),[ref])
        calls = [c for c in self.calls if c[0][1:3]==['generate','create']]
        self.assertEqual(len(calls),1)
        argv,env = calls[0]
        self.assertEqual(argv[argv.index('--prompt')+1],request['params']['prompt'])
        staged = Path(argv[argv.index('--image-references')+1])
        self.assertEqual(staged.suffix, '.png')
        self.assertFalse(staged.exists())
        self.assertTrue(ref.path.exists())
        self.assertNotIn('--wait',argv)
        self.assertEqual(set(env),{'HOME','PATH','NO_COLOR'})
        self.assertEqual(env['HOME'],str(self.home))
        self.assertEqual(result.state,'succeeded')
        self.assertIn('token=secret',result.result_url)
        self.assertNotIn('secret',json.dumps(result.raw_receipt))
        self.assertNotIn('secret',repr(result))

    def gpt_request(self):
        self.caps['gpt_image_2_5'] = {'job_type':'gpt_image_2_5', 'type':'image', 'params':[
            {'name':'prompt','type':'string','required':True},
            {'name':'resolution','type':'string','enum':['1k','2k','4k']},
            {'name':'aspect_ratio','type':'string','enum':['16:9','1:1']},
            {'name':'quality','type':'string','enum':['low','medium','high','xhigh','max']},
            {'name':'variant','type':'string','enum':['flare','sunburst']},
            {'name':'background','type':'string|null'},
            {'name':'image_references','type':'array'}]}
        self.provider = self.make()
        return {'job_type':'gpt_image_2_5', 'params':{'prompt':'A bright rehearsal room.',
            'resolution':'2k','aspect_ratio':'16:9','quality':'high','variant':'sunburst'},'references':[]}

    def test_gpt_exact_craft_settings_three_refs_and_returned_identity(self):
        request = self.gpt_request()
        refs = [self.reference(request) for _ in range(3)]
        self.response = CommandResult(0,json.dumps(self.job(request)).encode(),b'')
        receipt = self.provider.submit(self.intent(request), refs)
        self.assertEqual(receipt.job_type,'gpt_image_2_5')
        argv = self.calls[-1][0]
        for key, value in [('quality','high'),('variant','sunburst'),('resolution','2k')]:
            self.assertEqual(argv[argv.index('--'+key)+1],value)
        paths = [Path(argv[i+1]) for i,v in enumerate(argv) if v=='--image-references']
        self.assertEqual([p.suffix for p in paths], ['.png'] * 3)
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all(not p.exists() for p in paths))

    def test_large_opaque_stills_upload_as_jpeg_while_transparent_and_small_stay_exact(self):
        from PIL import Image
        request = self.request(True)
        refs = []
        for mode in ('RGB', 'RGBA', 'small'):
            ref = self.reference(request)
            size = (64, 64) if mode == 'small' else (900, 900)
            image = Image.frombytes('RGB', size, os.urandom(size[0]*size[1]*3)).convert('RGBA' if mode == 'RGBA' else 'RGB')
            if mode == 'RGBA':
                image.putpixel((0, 0), (0, 0, 0, 0))
            with ref.path.open('wb') as stream:
                image.save(stream, 'PNG')
            digest = hashlib.sha256(ref.path.read_bytes()).hexdigest()
            request['references'][-1]['sha256'] = digest
            refs.append(ResolvedReference(ref.object_ref, ref.path, digest, ref.media_type))
        self.assertGreater(refs[0].path.stat().st_size, 1_000_000)
        seen = []
        original = self.transport
        def inspect(argv, env, timeout, max_bytes):
            if argv[1:3] == ['generate','create']:
                for i, v in enumerate(argv):
                    if v == '--image-references':
                        path = Path(argv[i+1])
                        seen.append((path.suffix, path.stat().st_size))
            return original(argv, env, timeout, max_bytes)
        self.provider.transport = inspect
        self.response = CommandResult(0, json.dumps(self.job(request)).encode(), b'')
        self.provider.submit(self.intent(request), refs)
        self.assertEqual([s for s, _ in seen], ['.jpg', '.png', '.png'])
        self.assertLess(seen[0][1], refs[0].path.stat().st_size)
        self.assertEqual(seen[1][1], refs[1].path.stat().st_size)
        self.assertEqual(seen[2][1], refs[2].path.stat().st_size)

    def test_extensionless_references_have_exact_bytes_during_native_call_and_cleanup_on_error(self):
        request = self.request(True)
        refs = [self.reference(request,m) for m in ('image/jpeg','video/mp4','audio/wav')]
        refs = [ResolvedReference(r.object_ref, r.path.rename(r.path.with_suffix('')), r.sha256, r.media_type) for r in refs]
        paths = []
        original = self.transport
        def inspect(argv, env, timeout, max_bytes):
            if argv[1:3] == ['generate','create']:
                for flag, suffix, ref in zip(('--image-references','--video-references','--audio-references'),('.jpg','.mp4','.wav'),refs,strict=True):
                    path = Path(argv[argv.index(flag)+1]); paths.append(path)
                    self.assertEqual(path.suffix, suffix)
                    self.assertEqual(path.read_bytes(), ref.path.read_bytes())
                    self.assertEqual(path.stat().st_ino, ref.path.stat().st_ino)
                    self.assertFalse(path.is_symlink())
                raise TimeoutError('synthetic transport loss')
            return original(argv,env,timeout,max_bytes)
        self.provider.transport = inspect
        with self.assertRaises(DomainError):
            self.provider.submit(self.intent(request),refs)
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertTrue(all(r.path.exists() for r in refs))

    def test_unmapped_reference_mime_sends_no_command(self):
        request = self.request()
        ref = self.reference(request,'image/invented')
        with self.assertRaises(DomainError):
            self.provider.submit(self.intent(request),[ref])
        self.assertEqual(self.calls, [])

    def test_gpt_missing_settings_invalid_enum_and_out_of_scope_references_send_nothing(self):
        for mutation in ['quality','variant','bad-quality','background','four-images','audio']:
            with self.subTest(mutation=mutation):
                request = self.gpt_request()
                if mutation in ('quality','variant'):
                    del request['params'][mutation]
                elif mutation == 'bad-quality':
                    request['params']['quality']='invented'
                elif mutation == 'background':
                    request['params']['background']='transparent'
                refs = ([self.reference(request) for _ in range(4)] if mutation == 'four-images'
                        else [self.reference(request,'audio/wav')] if mutation == 'audio' else [])
                before = len(self.calls)
                with self.assertRaises(DomainError):
                    self.provider.submit(self.intent(request), refs)
                self.assertEqual(len(self.calls),before)

    def test_explicit_media_modalities_and_unsupported_mask_edit_mode(self):
        request = self.request(True)
        refs = [self.reference(request,m) for m in ('image/png','video/mp4','audio/wav')]
        self.response = CommandResult(0,json.dumps(self.job(request)).encode(),b'')
        self.provider.submit(self.intent(request),refs)
        argv = self.calls[-1][0]
        for name in ('--image-references','--video-references','--audio-references'):
            self.assertIn(name,argv)
        for extra in ({'mask':'/tmp/x'},{'edit_mode':'inpaint'},{'endpoint':'https://evil'},{'wait':True}):
            bad = self.request()
            bad['params'].update(extra)
            with self.assertRaises(DomainError):
                self.provider.submit(self.intent(bad),[])

    def test_reference_identity_hash_path_and_mime_are_bound(self):
        request = self.request()
        ref = self.reference(request)
        ref.path.write_bytes(b'tampered')
        with self.assertRaises(DomainError):
            self.provider.submit(self.intent(request),[ref])
        with self.assertRaises(DomainError):
            self.provider.submit(self.intent(request),[])
        request = self.request()
        ref = self.reference(request,'audio/wav')
        with self.assertRaises(DomainError):
            self.provider.submit(self.intent(request),[ref])
        self.assertFalse(any(c[0][1:3]==['generate','create'] for c in self.calls))

    def test_create_timeout_invalid_json_and_nonzero_are_unknown_never_retried(self):
        for response in (TimeoutError(), CommandResult(0,b'not json',b''),CommandResult(1,b'',b'token=secret')):
            self.calls.clear()
            self.response = response
            with self.assertRaises(DomainError) as error:
                self.provider.submit(self.intent(self.request()),[])
            self.assertEqual(error.exception.code,'unknown_outcome')
            self.assertNotIn('secret',str(error.exception))
            self.assertEqual(sum(c[0][1:3]==['generate','create'] for c in self.calls),1)

    def test_native_create_id_array_is_only_submission_identity(self):
        job_id='00000000-0000-0000-0000-000000000099'
        self.response=CommandResult(0,json.dumps([job_id]).encode())
        evidence=[]
        receipt=self.provider.submit(self.intent(self.request()),[],evidence_sink=evidence.append)
        self.assertEqual((receipt.job_id,receipt.state),(job_id,'submitted'))
        self.assertIsNone(receipt.result_url)
        self.assertIsNone(receipt.settled_cost)
        self.assertEqual(base64.b64decode(evidence[0]['stdout']['base64']),self.response.stdout)
        for ids in ([],[job_id,job_id],['not-a-uuid'],[{}]):
            self.response=CommandResult(0,json.dumps(ids).encode())
            with self.subTest(ids=ids), self.assertRaises(DomainError) as error:
                self.provider.submit(self.intent(self.request()),[])
            self.assertEqual(error.exception.code,'unknown_outcome')

    def test_live_mode_cannot_use_fake_intent_or_injected_transport(self):
        with self.assertRaises(ValueError):
            self.make(live_enabled=True)
        real=HFProvider(self.caps,self.pin,service_home=self.home,service_uid=os.getuid(),
                        media_root=self.root,live_enabled=True)
        with self.assertRaises(DomainError) as error:
            real.submit(self.intent(self.request()),[])
        self.assertEqual(error.exception.code,'forbidden')
        self.assertEqual(self.calls,[])

    def test_raw_sink_precedes_decode_and_retains_unrecognized_envelope_privately(self):
        for raw in (b'not json', b'\xff', b'["remote-id"]', b'{"unknown_wrapper":{"ids":["remote-id"]}}'):
            self.response = CommandResult(0,raw,b'private diagnostic token=secret')
            evidence = []
            with self.assertRaises(DomainError) as error:
                self.provider.submit(self.intent(self.request()),[],evidence_sink=evidence.append)
            self.assertEqual(error.exception.code,'unknown_outcome')
            self.assertEqual(len(evidence),1)
            self.assertEqual(base64.b64decode(evidence[0]['stdout']['base64']),raw)
            self.assertEqual(base64.b64decode(evidence[0]['stderr']['base64']),self.response.stderr)
            self.assertEqual(evidence[0]['stage'],'native-command')
            self.assertEqual(evidence[0]['operation'],'create')
            self.assertIsNone(evidence[0]['expected_job_id'])
            self.assertNotIn('secret',str(error.exception))

    def test_native_partial_timeout_and_overflow_are_bounded_and_journalable(self):
        cases = [
            ('import os,time; os.write(1,b"partial-out"); os.write(2,b"partial-err"); time.sleep(10)', NativeTimeout, 'timeout'),
            ('import os; os.write(1,b"x"*65536)', NativeOutputLimit, 'output_limit'),
        ]
        for script, error_type, termination in cases:
            with self.subTest(termination=termination):
                with self.assertRaises(error_type) as native:
                    _native([sys.executable,'-c',script],{'PATH':'/usr/bin:/bin'},0.3,1024)
                partial = native.exception.result
                self.assertLessEqual(len(partial.stdout)+len(partial.stderr),1024)
                if termination == 'timeout':
                    self.assertEqual(partial.stdout,b'partial-out')
                    self.assertEqual(partial.stderr,b'partial-err')
                self.response = native.exception
                evidence = []
                with self.assertRaises(DomainError) as failure:
                    self.provider.submit(self.intent(self.request()),[],evidence_sink=evidence.append)
                self.assertEqual(failure.exception.code,'unknown_outcome')
                self.assertEqual(len(evidence),1)
                self.assertEqual(evidence[0]['termination'],termination)
                self.assertEqual(evidence[0]['truncated'],termination == 'output_limit')
                self.assertEqual(base64.b64decode(evidence[0]['stdout']['base64']),partial.stdout)
                self.assertEqual(evidence[0]['stdout']['sha256'],hashlib.sha256(partial.stdout).hexdigest())
                self.assertNotIn('partial-out',repr(native.exception))
                self.assertNotIn('partial-out',repr(partial))

    def test_nonzero_and_generic_timeout_preserve_evidence_without_retry(self):
        for response, termination in ((CommandResult(7,b'unknown id',b'token=secret'),'exited'),
                                      (TimeoutError(),'timeout'),
                                      (subprocess.TimeoutExpired('not-retained',1,output=b'partial',stderr=b'err'),'timeout')):
            self.response = response
            before = len([c for c in self.calls if c[0][1:3]==['generate','create']])
            evidence = []
            with self.assertRaises(DomainError):
                self.provider.submit(self.intent(self.request()),[],evidence_sink=evidence.append)
            self.assertEqual(len(evidence),1)
            self.assertEqual(evidence[0]['termination'],termination)
            self.assertNotIn('not-retained',json.dumps(evidence))
            self.assertEqual(len([c for c in self.calls if c[0][1:3]==['generate','create']]),before+1)

    def test_a_finished_take_settles_at_the_pinned_credits_per_second(self):
        """Higgsfield reports no bill per job, so a finished take settles at the pinned
        price (12 credits/s at 1080p, `higgsfield generate cost` 2026-09-27) x its seconds; no price, no settlement."""
        request = self.request(video=True)
        request['params']['mode'] = 't2v'
        priced = self.make(credits_per_second={'seedance_2_5': {'1080p': 12}})
        self.response = CommandResult(0,json.dumps(self.job(request)).encode(),b'')
        self.assertEqual(priced.get('remote-job-1',request).settled_cost, 36)
        self.assertIsNone(self.provider.get('remote-job-1',request).settled_cost)
        blocked = {**self.job(request), 'status': 'ip_detected', 'result_url': None}
        self.response = CommandResult(0,json.dumps(blocked).encode(),b'')
        # Higgsfield refunds a take its IP check stops (seen live 2026-09-27), so it settles at 0, not the hold.
        self.assertEqual((priced.get('remote-job-1',request).state, priced.get('remote-job-1',request).settled_cost), ('failed', 0))

    def test_get_binding_unique_calls_and_no_command_or_environment_in_evidence(self):
        evidence = []
        for _ in range(2):
            self.provider.get('remote-job-1',self.request(),evidence_sink=evidence.append)
        self.assertNotEqual(evidence[0]['invocation_id'],evidence[1]['invocation_id'])
        for item in evidence:
            self.assertEqual(item['operation'],'get')
            self.assertEqual(item['expected_job_id'],'remote-job-1')
            self.assertEqual(item['executable'],{'sha256':self.pin.sha256,'version':self.pin.version})
            self.assertNotIn(str(self.home),json.dumps(item))
            self.assertNotIn(str(self.binary),json.dumps(item))
            self.assertEqual(item['termination'],'exited')
        # A per-call sink must not be reused by a later call.
        self.provider.get('remote-job-1',self.request())
        self.assertEqual(len(evidence),2)

    def test_sink_failure_stops_before_json_and_receipt_interpretation(self):
        self.response = CommandResult(0,b'invalid json',b'private')
        def reject(_evidence):
            raise DomainError('unknown_outcome','Private journal unavailable')
        with patch.object(self.provider,'_receipt') as receipt,\
                patch('production.provider_hf.json.loads',side_effect=AssertionError('decoded before journal')),\
                self.assertRaises(DomainError) as error:
            self.provider.submit(self.intent(self.request()),[],evidence_sink=reject)
        self.assertEqual(error.exception.message,'Private journal unavailable')
        receipt.assert_not_called()

    def test_fake_oversized_output_is_clipped_with_explicit_truncation(self):
        self.provider = self.make(max_output_bytes=1024)
        self.response = CommandResult(0,b'x'*900,b'y'*900)
        evidence = []
        with self.assertRaises(DomainError):
            self.provider.get('remote-job-1',self.request(),evidence_sink=evidence.append)
        item = evidence[0]
        self.assertTrue(item['truncated'])
        self.assertEqual(item['termination'],'output_limit')
        self.assertEqual(item['stdout']['size']+item['stderr']['size'],1024)

    def test_valid_reported_conflicts_remain_critical(self):
        for params in ({'resolution':'1k'}, {'aspect_ratio':'1:1'}, {'prompt':'Different scene'}):
            job = self.job()
            job['params'] = params
            self.response = CommandResult(0,json.dumps(job).encode(),b'')
            result = self.provider.get('remote-job-1',self.request())
            self.assertEqual(result.state,'failed')
            self.assertTrue(result.critical_adjustments)
            self.assertIsNone(result.result_url)
            self.assertEqual(result.job_id,'remote-job-1')

    def test_missing_mode_report_does_not_invent_a_parameter_change(self):
        request = self.request(True)
        self.reference(request)
        job = self.job(request)
        job['params'] = {key:value for key,value in request['params'].items() if key != 'mode'}
        self.response = CommandResult(0,json.dumps(job).encode(),b'')
        result = self.provider.get('remote-job-1',request)
        self.assertEqual(result.state,'succeeded')
        self.assertEqual(result.critical_adjustments,{})
        self.assertEqual(result.parameter_reports['mode']['status'],'not_reported')
        self.assertEqual(result.parameter_reports['resolution']['status'],'matched')
        self.assertIsNotNone(result.result_url)
        self.assertEqual(sum(c[0][1:3]==['generate','get'] for c in self.calls),1)

    def test_absent_null_invalid_type_or_enum_reports_are_unverified_not_changes(self):
        for params, status in (({},'not_reported'),({'resolution':None},'unverifiable'),
                               ({'resolution':42},'unverifiable'),({'resolution':'8k'},'unverifiable')):
            with self.subTest(params=params):
                job = self.job()
                job['params'] = params
                self.response = CommandResult(0,json.dumps(job).encode(),b'')
                result = self.provider.get('remote-job-1',self.request())
                self.assertEqual(result.state,'succeeded')
                self.assertEqual(result.critical_adjustments,{})
                self.assertEqual(result.parameter_reports['resolution']['status'],status)
                self.assertNotIn('verified',result.parameter_reports['resolution'])
        request = self.request(True)
        self.reference(request)
        for invalid in (True,0,-1,'3'):
            job = self.job(request)
            job['params'] = {**request['params'],'duration':invalid}
            self.response = CommandResult(0,json.dumps(job).encode(),b'')
            result = self.provider.get('remote-job-1',request)
            self.assertEqual(result.parameter_reports['duration']['status'],'unverifiable')
            self.assertEqual(result.critical_adjustments,{})

    def test_extra_reports_do_not_change_authorized_parameters_and_are_redacted(self):
        request = self.request()
        job = self.job(request)
        job['params'] = {**request['params'],'debug_url':'https://cdn.example/x?token=private-token'}
        self.response = CommandResult(0,json.dumps(job).encode(),b'')
        result = self.provider.get('remote-job-1',request)
        self.assertEqual(result.state,'succeeded')
        self.assertEqual(set(result.parameter_reports),set(request['params']))
        self.assertIn('debug_url',result.adjustments)
        self.assertNotIn('private-token',json.dumps(result.adjustments))
        self.assertNotIn('debug_url',request['params'])

    def test_unknown_status_and_blocked_status_remain_distinct(self):
        for status,expected in [('novel-provider-state','unknown'),('ip_detected','failed'),('queued','running')]:
            job = self.job()
            job['status']=status
            self.response=CommandResult(0,json.dumps(job).encode(),b'')
            self.assertEqual(self.provider.get('remote-job-1',self.request()).state,expected)
        with self.assertRaises(DomainError):
            self.provider.get('--endpoint=evil',self.request())

    def test_discovery_and_cost_never_auto_upload_local_references(self):
        self.assertEqual(self.provider.capabilities('nano_banana_pro'),self.caps['nano_banana_pro'])
        request=self.request()
        self.response=CommandResult(0,b'{"credits":7}',b'')
        quote=self.provider.cost(request)
        self.assertFalse(quote['authoritative'])
        self.assertIsNone(quote['settled_cost'])
        self.reference(request)
        count=len(self.calls)
        with self.assertRaises(DomainError):
            self.provider.cost(request)
        self.assertEqual(len(self.calls),count)

    def test_real_subprocess_is_bounded_without_provider_or_shell(self):
        env = {'PATH':'/usr/bin:/bin'}
        result = _native([sys.executable,'-c','print("bounded")'],env,2,1024)
        self.assertEqual(result.stdout.strip(),b'bounded')
        with self.assertRaises(ValueError):
            _native([sys.executable,'-c','import os; os.write(1,b"x"*65536)'],env,2,1024)
        with self.assertRaises(TimeoutError):
            _native([sys.executable,'-c','import time; time.sleep(10)'],env,0.05,1024)

    def test_timeout_terminates_descendant_after_group_leader_exits(self):
        marker = self.root/'descendant.pid'
        child = ('import os, pathlib, sys, time; '
                 'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(10)')
        leader = ('import os, pathlib, subprocess, sys, time\n'
                  'subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])\n'
                  'while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.005)\n'
                  'os._exit(0)\n')
        argv = [sys.executable, '-c', leader, child, str(marker)]
        original = subprocess.Popen
        def started(args, *positional, **kwargs):
            process = original(args, *positional, **kwargs)
            if list(args) == argv:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not marker.exists():
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                    raise AssertionError('Descendant did not acknowledge startup')
            return process
        try:
            with patch('subprocess.Popen', side_effect=started), self.assertRaises(TimeoutError):
                _native(argv,
                        {'PATH': '/usr/bin:/bin'}, 1, 1024)
            self.assertTrue(marker.exists(), 'Descendant must acknowledge startup before the leader exits')
            pid = int(marker.read_text())
            deadline = time.monotonic() + 1
            while True:
                if sys.platform.startswith('linux'):
                    try:
                        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
                    except FileNotFoundError:
                        state = ''
                else:
                    state = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(pid)],
                                           capture_output=True, text=True, timeout=2, check=False).stdout.strip()
                if not state or state.startswith('Z') or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertTrue(not state or state.startswith('Z'), f'Descendant survived timeout: {state}')
        finally:
            if marker.exists():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_unreapable_native_child_preserves_partial_evidence_and_fatal_dominates_sink_failure(self):
        for sink_failure in (None, DomainError('unknown_outcome','Journal unavailable'), RuntimeError('journal failed')):
            with self.subTest(sink_failure=type(sink_failure).__name__):
                process, selector, key = MagicMock(), MagicMock(), MagicMock()
                process.pid = 123456
                process.stdout.fileno.return_value = 11
                process.stderr.fileno.return_value = 12
                process.wait.side_effect = subprocess.TimeoutExpired('private-command',2)
                key.fd = 11
                selector.select.return_value = [(key,1)]
                selector.close.side_effect = OSError('closed selector')
                process.stdout.close.side_effect = OSError('closed pipe')
                evidence = []
                def sink(value, records=evidence, failure=sink_failure):
                    records.append(value)
                    if failure is not None:
                        raise failure
                provider = self.make(timeout=0.1)
                provider._version_checked = True
                provider.transport = _native
                with patch.object(review_http,'_POISONED',False),\
                        patch('production.provider_hf.subprocess.Popen',return_value=process) as launch,\
                        patch('production.provider_hf.selectors.DefaultSelector',return_value=selector),\
                        patch('production.provider_hf.os.set_blocking'),\
                        patch('production.provider_hf.os.read',return_value=b'partial-private-command'),\
                        patch.object(review_http.os,'killpg') as kill,\
                        patch('production.provider_hf.time.monotonic',side_effect=[0,0,0.2,0.2,0.2]):
                    with self.assertRaises(review_http.FatalWorkerError) as fatal:
                        provider.submit(self.intent(self.request()),[],evidence_sink=sink)
                    self.assertNotIn('private-command',str(fatal.exception))
                    self.assertFalse(review_http.is_healthy())
                    self.assertEqual(len(evidence),1)
                    self.assertEqual(base64.b64decode(evidence[0]['stdout']['base64']),b'partial-private-command')
                    self.assertEqual(evidence[0]['termination'],'timeout')
                    self.assertTrue(evidence[0]['cleanup_failed'])
                    kill.assert_called_once_with(process.pid,signal.SIGKILL)
                    self.assertTrue(0 < process.wait.call_args.kwargs['timeout'] <= 2)
                    selector.close.assert_called_once()
                    process.stdout.close.assert_called_once()
                    process.stderr.close.assert_called_once()
                    with self.assertRaises(review_http.FatalWorkerError):
                        provider.get('remote-job-1',self.request())
                    with self.assertRaises(review_http.FatalWorkerError):
                        _native([str(self.binary)],{},1,1024)
                    launch.assert_called_once()
                self.assertTrue(review_http.is_healthy())

    def test_selector_setup_failure_still_uses_bounded_cleanup_and_closes_pipes(self):
        process = MagicMock()
        process.pid = 123456
        process.stdout.fileno.return_value = 11
        process.stderr.fileno.return_value = 12
        process.wait.return_value = -9
        with patch('production.provider_hf.subprocess.Popen',return_value=process),\
                patch('production.provider_hf.selectors.DefaultSelector',side_effect=OSError('No selectors')),\
                patch.object(review_http.os,'killpg') as kill, self.assertRaises(OSError):
            _native([str(self.binary)],{},1,1024)
        kill.assert_called_once_with(process.pid,signal.SIGKILL)
        self.assertTrue(0 < process.wait.call_args.kwargs['timeout'] <= 2)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_shared_poison_prevents_native_or_injected_supplier_launch(self):
        with patch.object(review_http,'_POISONED',True),\
                patch('production.provider_hf.subprocess.Popen',side_effect=AssertionError('No new process')),\
                self.assertRaises(review_http.FatalWorkerError):
            _native([sys.executable,'-c','pass'],{},1,1024)
        with patch.object(review_http,'_POISONED',True), self.assertRaises(review_http.FatalWorkerError):
            self.provider.get('remote-job-1',self.request())
        self.assertEqual(self.calls,[])

    def test_changed_capability_output_limit_and_result_id_fail_closed(self):
        self.caps['nano_banana_pro']['new_provider_rule'] = 'Changed after release'
        with self.assertRaises(DomainError) as changed:
            self.provider.capabilities('nano_banana_pro')
        self.assertEqual(changed.exception.code,'release_mismatch')
        self.response = CommandResult(0,b'x'*(1048576+1),b'')
        with self.assertRaises(DomainError):
            self.provider.get('remote-job-1',self.request())
        job = self.job()
        job['id'] = 'another-job'
        self.response = CommandResult(0,json.dumps(job).encode(),b'')
        with self.assertRaises(DomainError):
            self.provider.get('remote-job-1',self.request())

    def test_live_create_unverified_and_executable_tampering_block_before_transport(self):
        real = HFProvider(self.caps,self.pin,service_home=self.home,service_uid=os.getuid(),media_root=self.root)
        with self.assertRaises(DomainError) as error:
            real.submit({'operation':'submit','request':self.request(),'cost':{'mode':'live'}},[])
        self.assertEqual(error.exception.code,'unsupported_route')
        self.binary.write_bytes(b'changed binary')
        with self.assertRaises(DomainError):
            self.provider.capabilities('nano_banana_pro')
        self.assertEqual(self.calls,[])


if __name__=='__main__':
    unittest.main()
