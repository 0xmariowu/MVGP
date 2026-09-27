"""Durable fenced effects and bounded, pinned result downloads; no live calls."""
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from production.auth import SYSTEM_PROJECT
from production.contracts import DomainError, content_hash, new_id
from production.jobs import Download, Jobs, SafeDownloader, _http
from production.provider_types import ProviderReceipt
from production.tests import test_submissions
from production.tests.fixtures import hf_era_document


class JobTests(unittest.TestCase):
    def setUp(self):
        self.s = test_submissions.SubmissionTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.f,self.store,self.pid = self.s.f,self.s.store,self.s.pid
        self.now = 1000.0
        self.worker = self.f.auth.authenticate(self.f.auth.provision_token('worker_a','worker',[self.pid],300))
        self.other = self.f.auth.authenticate(self.f.auth.provision_token('worker_b','worker',[self.pid],300))
        data = io.BytesIO()
        Image.new('RGB',(2048,1152),'red').save(data,format='PNG')
        self.png = data.getvalue()
        @contextmanager
        def download(url,host,ip,timeout):
            yield Download('image/png',[self.png])
        self.downloader = SafeDownloader({'cdn.example'}, resolver=lambda host:['8.8.8.8'],transport=download)
        self.jobs = Jobs(self.store,self.f.auth,self.s.service,self.f.media,downloader=self.downloader,clock=lambda:self.now,lease_seconds=20,max_polls=2,poll_delay=1)
        self.job = self.s.service.submit(self.s.actor,self.pid,self.s.request())

    def ref(self,obj):
        return self.f.ref(obj)

    def claim(self,worker=None):
        return self.jobs.claim(worker or self.worker,self.pid,self.job['object_id'])

    def dispatched(self):
        claim = self.claim()
        return self.jobs.begin_dispatch(self.worker,self.pid,self.ref(claim['job']),claim['fence'])

    def receipt(self,state='succeeded',cost=None):
        return ProviderReceipt('remote1','nano_banana_pro','completed' if state=='succeeded' else 'unverified',state,
            {'id':'remote1','status':state},{},{},'https://cdn.example/result.png?token=private' if state=='succeeded' else None,cost)

    def test_two_workers_claim_once_and_only_one_dispatch_journal(self):
        barrier = threading.Barrier(2)
        def claim(worker):
            barrier.wait()
            try:
                return self.jobs.claim(worker,self.pid,self.job['object_id'])
            except DomainError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim,[self.worker,self.other]))
        self.assertEqual(sum(c is not None for c in claims),1)
        c = next(c for c in claims if c)
        worker = self.worker if c['job']['body']['lease']['credential_id']==self.worker.credential_id else self.other
        dispatched = self.jobs.begin_dispatch(worker,self.pid,self.ref(c['job']),c['fence'])
        self.assertEqual(dispatched['job']['body']['state'],'dispatching')
        self.assertEqual(dispatched['intent']['body']['request'],self.s.candidate['body']['request'])
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),1)
        with self.assertRaises(DomainError):
            self.jobs.begin_dispatch(worker,self.pid,self.ref(c['job']),c['fence'])

    def test_restart_before_dispatch_reclaims_but_after_dispatch_quarantines(self):
        old = self.claim()
        self.now += 21
        new = self.claim(self.other)
        self.assertGreater(new['fence'],old['fence'])
        with self.assertRaises(DomainError):
            self.jobs.begin_dispatch(self.worker,self.pid,self.ref(old['job']),old['fence'])
        self.jobs.begin_dispatch(self.other,self.pid,self.ref(new['job']),new['fence'])
        self.now += 21
        quarantined = self.claim()
        self.assertEqual(quarantined['action'],'quarantined')
        self.assertEqual(quarantined['job']['body']['state'],'unknown')
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),1)

    def test_revoked_origin_blocks_same_transaction_dispatch(self):
        claim = self.claim()
        self.f.auth.revoke(self.s.actor.credential_id)
        with self.assertRaises(DomainError):
            self.jobs.begin_dispatch(self.worker,self.pid,self.ref(claim['job']),claim['fence'])
        # A revoked maker never comes back: the unsent job is cancelled and its hold released (bug hunt 2026-09-25).
        self.assertEqual(self.store.get_object(self.pid,self.job['object_id'])['body']['state'],'cancelled')
        self.assertEqual(self.store.budget(self.pid)['reserved'],0)
        self.assertEqual(self.store.list_objects(self.pid,kind='provider-attempt'),[])

    def test_unknown_quarantine_claims_do_not_write_revisions_or_events(self):
        for fields in ({'pending_result': {'object_id': 'receipt'}, 'download_count': self.jobs.max_downloads},
                       {'remote_job_id': 'remote1', 'poll_count': self.jobs.max_polls}, {}):
            for lease in (None, {'expires_at': self.now - 1}):
                with self.subTest(fields=fields, lease=lease):
                    job = self.store.create_object(self.pid, 'job',
                        {'state': 'unknown', 'lease': lease, **fields}, 'worker_service')
                    events = self.store.events(self.pid)
                    for _ in range(2):
                        with self.assertRaises(DomainError) as caught:
                            self.jobs.claim(self.worker, self.pid, job['object_id'])
                        self.assertEqual(caught.exception.code, 'revision_conflict')
                        self.assertEqual(self.store.get_object(self.pid, job['object_id']), job)
                        self.assertEqual(self.store.events(self.pid), events)

    def test_running_spent_allowance_quarantines_once(self):
        for fields in ({'pending_result': {'object_id': 'receipt'}, 'download_count': self.jobs.max_downloads},
                       {'remote_job_id': 'remote1', 'poll_count': self.jobs.max_polls}):
            with self.subTest(fields=fields):
                job = self.store.create_object(self.pid, 'job',
                    {'state': 'running', 'lease': None, **fields}, 'worker_service')
                claim = self.jobs.claim(self.worker, self.pid, job['object_id'])
                self.assertEqual(claim['action'], 'quarantined')
                self.assertIsNone(claim['fence'])
                self.assertEqual(claim['job']['revision'], job['revision'] + 1)
                self.assertEqual(claim['job']['body'], {**job['body'], 'state': 'unknown'})
                events = self.store.events(self.pid)
                self.assertEqual(events[-1]['kind'], 'job.quarantined')
                with self.assertRaises(DomainError) as caught:
                    self.jobs.claim(self.worker, self.pid, job['object_id'])
                self.assertEqual(caught.exception.code, 'revision_conflict')
                self.assertEqual(self.store.get_object(self.pid, job['object_id']), claim['job'])
                self.assertEqual(self.store.events(self.pid), events)

    def test_unknown_remaining_download_allowance_is_claimed(self):
        dispatch = self.dispatched()
        received = self.jobs.record_receipt(self.worker, self.pid, self.ref(dispatch['job']), dispatch['fence'], self.receipt())
        job = self.store.append_revision(self.pid, received['object_id'], received['revision'],
            {**received['body'], 'state': 'unknown', 'lease': None, 'download_count': self.jobs.max_downloads - 1}, 'worker_service')
        claim = self.claim()
        self.assertEqual(claim['action'], 'download')
        self.assertEqual(claim['job']['body']['state'], 'unknown')
        self.assertEqual(claim['job']['body']['pending_result'], job['body']['pending_result'])
        self.assertEqual(claim['job']['body']['download_count'], self.jobs.max_downloads - 1)
        self.assertEqual(claim['job']['body']['lease']['credential_id'], self.worker.credential_id)

    def test_receipt_is_durable_before_download_restart_and_actual_dimensions(self):
        dispatch = self.dispatched()
        received = self.jobs.record_receipt(self.worker,self.pid,self.ref(dispatch['job']),dispatch['fence'],self.receipt())
        self.assertNotEqual(received['body']['state'],'succeeded')
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-receipt')),1)
        self.assertNotIn('private',str(received))
        from production.projects import Projects
        public = Projects(self.store, self.f.auth, self.f.media, label='lean-v1').list(self.s.actor, self.pid)
        self.assertNotIn('token=private', str(public))
        self.now += 21
        claim = self.claim(self.other)
        self.assertEqual(claim['action'],'download')
        result = self.jobs.download_result(self.other,self.pid,self.ref(claim['job']),claim['fence'])
        media = self.store.get_object(self.pid,result['body']['result']['object_id'])
        self.assertEqual(result['body']['state'],'succeeded')
        self.assertEqual(media['body']['probe']['width'],2048)
        self.assertEqual(media['body']['probe']['height'],1152)
        self.assertFalse(media['body']['provenance']['native_resolution_verified'])
        conformance = media['body']['provenance']['output_conformance']
        self.assertTrue(conformance['passed'])
        self.assertEqual(conformance['requested']['minimum_pixels'], 2048)
        self.assertEqual(conformance['observed']['width'], 2048)
        self.assertFalse(conformance['native_resolution_verified'])
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)
        self.assertEqual(self.store.budget(self.pid)['spent'],0)

    def test_undersized_result_is_publicly_failed_without_media_ref_or_refund(self):
        from production.queries import Queries
        data = io.BytesIO()
        Image.new('RGB', (12, 8), 'red').save(data, format='PNG')
        self.png = data.getvalue()
        dispatch = self.dispatched()
        received = self.jobs.record_receipt(self.worker, self.pid, self.ref(dispatch['job']), dispatch['fence'], self.receipt())
        before = self.store.budget(self.pid)
        result = self.jobs.download_result(self.worker, self.pid, self.ref(received), dispatch['fence'])
        self.assertEqual(result['body']['state'], 'failed')
        self.assertIsNone(result['body'].get('result'))
        self.assertIsNone(result['body']['pending_result'])
        self.assertIsNone(result['body']['lease'])
        self.assertEqual(result['body']['cost_status'], 'unknown')
        self.assertEqual(self.store.budget(self.pid), before)
        self.assertFalse([o for o in self.store.list_objects(self.pid, kind='media') if o['author'] == 'worker_service'])
        evidence = self.store.list_objects(SYSTEM_PROJECT, kind='output-evidence')
        self.assertEqual(len(evidence), 1)
        body = evidence[0]['body']
        self.assertEqual(body['intent'], self.ref(dispatch['intent']).model_dump())
        self.assertEqual(body['attempt']['object_id'], dispatch['job']['body']['attempt_id'])
        self.assertEqual(body['metadata']['sha256'], hashlib.sha256(self.png).hexdigest())
        self.assertEqual(body['metadata']['probe']['width'], 12)
        self.assertFalse(body['conformance']['passed'])
        view = Queries(self.store, self.f.auth, self.f.flow).artifact(self.s.actor, self.pid, result['object_id'])
        error = view['details']['last_error']
        self.assertEqual(error['code'], 'delivered_output_nonconforming')
        self.assertIn('minimum_edge_mismatch', str(error['reasons']))
        self.assertNotIn('token=private', json.dumps(view))
        self.assertNotIn('storage_manifest', json.dumps(view))
        with self.assertRaises(DomainError):
            Queries(self.store, self.f.auth, self.f.flow).artifact(self.s.actor, self.pid, evidence[0]['object_id'])
        with self.assertRaises(DomainError):
            self.claim()
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 1)

    def test_failed_output_evidence_transaction_rolls_back_and_only_download_retries(self):
        data = io.BytesIO()
        Image.new('RGB', (12, 8), 'red').save(data, format='PNG')
        self.png = data.getvalue()
        dispatch = self.dispatched()
        received = self.jobs.record_receipt(self.worker, self.pid, self.ref(dispatch['job']), dispatch['fence'], self.receipt())
        append = self.store.append_revision
        def fail(pid, oid, revision, body, author, **kwargs):
            if oid == self.job['object_id'] and body.get('state') == 'failed':
                raise DomainError('revision_conflict', 'Injected metadata commit failure')
            return append(pid, oid, revision, body, author, **kwargs)
        with patch.object(self.store, 'append_revision', side_effect=fail), self.assertRaises(DomainError):
            self.jobs.download_result(self.worker, self.pid, self.ref(received), dispatch['fence'])
        self.assertEqual(self.store.list_objects(SYSTEM_PROJECT, kind='output-evidence'), [])
        self.assertEqual(self.store.get_object(self.pid, self.job['object_id'])['body']['state'], 'unknown')
        claim = self.claim()
        self.assertEqual(claim['action'], 'download')
        result = self.jobs.download_result(self.worker, self.pid, self.ref(claim['job']), claim['fence'])
        self.assertEqual(result['body']['state'], 'failed')
        self.assertEqual(len(self.store.list_objects(SYSTEM_PROJECT, kind='output-evidence')), 1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 1)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 10)

    def test_download_uses_the_frozen_intent_route_not_the_current_config(self):
        dispatch = self.dispatched()
        received = self.jobs.record_receipt(self.worker, self.pid, self.ref(dispatch['job']), dispatch['fence'], self.receipt())
        intent = dispatch['intent']
        self.store.append_revision(self.pid, intent['object_id'], 1, {**intent['body'], 'route':{}}, 'fixture_operator')
        self.f.profile['output_contract']['minimum_pixels_by_resolution']['2k'] = 4096
        self.f.config.set('image_routes', self.f.routes)
        self.s.c.activate()
        result = self.jobs.download_result(self.worker, self.pid, self.ref(received), dispatch['fence'])
        self.assertEqual(result['body']['state'], 'succeeded')
        media = self.store.get_object(self.pid, result['body']['result']['object_id'])
        self.assertEqual(media['body']['provenance']['intent'], self.ref(intent).model_dump())
        self.assertEqual(media['body']['provenance']['output_conformance']['requested']['minimum_pixels'], 2048)
        self.assertFalse(result['body']['current'])

    def test_rejected_output_preserves_already_settled_supplier_cost(self):
        data = io.BytesIO()
        Image.new('RGB', (12, 8), 'red').save(data, format='PNG')
        self.png = data.getvalue()
        dispatch = self.dispatched()
        received = self.jobs.record_receipt(self.worker, self.pid, self.ref(dispatch['job']), dispatch['fence'], self.receipt(cost=8))
        result = self.jobs.download_result(self.worker, self.pid, self.ref(received), dispatch['fence'])
        self.assertEqual(result['body']['state'], 'failed')
        self.assertEqual(result['body']['cost_status'], 'settled')
        self.assertEqual(self.store.budget(self.pid)['spent'], 8)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)

    def test_stale_fence_cannot_publish_returned_result(self):
        dispatch = self.dispatched()
        self.now += 21
        self.claim(self.other)
        with self.assertRaises(DomainError):
            self.jobs.record_receipt(self.worker,self.pid,self.ref(dispatch['job']),dispatch['fence'],self.receipt())
        self.assertEqual(self.store.list_objects(self.pid,kind='provider-receipt'),[])

    def test_known_id_unknown_status_polls_only_and_attempts_are_bounded(self):
        dispatched = self.dispatched()
        unknown = self.jobs.record_receipt(self.worker,self.pid,self.ref(dispatched['job']),dispatched['fence'],self.receipt('unknown'))
        self.assertEqual(unknown['body']['remote_job_id'],'remote1')
        for _ in range(2):
            self.now += 10
            claim = self.claim(self.other)
            self.assertEqual(claim['action'],'poll')
            self.jobs.poll_failed(self.other,self.pid,self.ref(claim['job']),claim['fence'])
        self.now += 10
        before = self.store.get_object(self.pid, self.job['object_id'])
        with self.assertRaises(DomainError) as caught:
            self.claim()
        self.assertEqual(caught.exception.code, 'revision_conflict')
        self.assertEqual(self.store.get_object(self.pid, self.job['object_id']), before)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),1)

    def test_cancel_before_call_is_zero_cost_but_inflight_is_not_refund(self):
        cancelled = self.jobs.cancel(self.worker,self.pid,self.ref(self.job))
        self.assertEqual(cancelled['body']['state'],'cancelled')
        self.assertEqual(self.store.budget(self.pid)['reserved'],0)
        self.job = self.s.service.submit(self.s.actor,self.pid,self.s.request('again'))
        dispatched = self.dispatched()
        requested = self.jobs.cancel(self.worker,self.pid,self.ref(dispatched['job']))
        self.assertTrue(requested['body']['cancel_requested'])
        self.assertEqual(requested['body']['state'],'dispatching')
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_cost_overrun_is_recorded_without_erasing_spend(self):
        self.store.set_budget(self.pid,10,'credit')
        dispatched = self.dispatched()
        self.jobs.record_receipt(self.worker,self.pid,self.ref(dispatched['job']),dispatched['fence'],self.receipt('unknown',15))
        budget = self.store.budget(self.pid)
        self.assertEqual(budget['spent'],15)
        self.assertEqual(budget['reserved'],0)
        with self.assertRaises(DomainError):
            self.s.service.submit(self.s.actor,self.pid,self.s.request('overrun'))

    def test_edit_after_dispatch_preserves_request_but_marks_result_non_current(self):
        dispatched = self.dispatched()
        self.store.append_revision(self.pid,self.s.target['object_id'],1,self.s.target['body'],'author')
        received = self.jobs.record_receipt(self.worker,self.pid,self.ref(dispatched['job']),dispatched['fence'],self.receipt())
        result = self.jobs.download_result(self.worker,self.pid,self.ref(received),dispatched['fence'])
        self.assertEqual(result['body']['state'],'succeeded')
        self.assertFalse(result['body']['current'])
        self.assertIn(self.s.target['object_id'],str(result['body']['non_current_reasons']))
        stored = self.store.get_object(self.pid,dispatched['intent']['object_id'])
        self.assertEqual(stored,dispatched['intent'])

    def test_expired_fence_during_download_cannot_publish_media(self):
        dispatched = self.dispatched()
        received = self.jobs.record_receipt(self.worker,self.pid,self.ref(dispatched['job']),dispatched['fence'],self.receipt())
        @contextmanager
        def expire(url,host,ip,timeout):
            def chunks():
                yield self.png
                self.now += 21
                self.claim(self.other)
            yield Download('image/png',chunks())
        self.jobs.downloader = SafeDownloader({'cdn.example'},resolver=lambda host:['8.8.8.8'],transport=expire)
        with self.assertRaises(DomainError):
            self.jobs.download_result(self.worker,self.pid,self.ref(received),dispatched['fence'])
        outputs = [m for m in self.store.list_objects(self.pid,kind='media') if m['author']=='worker_service']
        self.assertEqual(outputs,[])
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-receipt')),1)

    def test_author_cannot_claim_or_record_worker_results(self):
        with self.assertRaises(DomainError):
            self.jobs.claim(self.s.actor,self.pid,self.job['object_id'])

    def test_valid_document_download_cannot_complete_an_image_job(self):
        dispatched = self.dispatched()
        received = self.jobs.record_receipt(self.worker,self.pid,self.ref(dispatched['job']),dispatched['fence'],self.receipt())
        @contextmanager
        def wrong_type(url,host,ip,timeout):
            yield Download('application/json', [b'{"error":"not an image"}'])
        self.jobs.downloader = SafeDownloader({'cdn.example'}, resolver=lambda host:['8.8.8.8'], transport=wrong_type)
        result = self.jobs.download_result(self.worker,self.pid,self.ref(received),dispatched['fence'])
        self.assertEqual(result['body']['state'], 'failed')
        self.assertIn('modality_mismatch', str(result['body']['last_error']))
        self.assertFalse([m for m in self.store.list_objects(self.pid,kind='media') if m['author']=='worker_service'])


class DownloadTests(unittest.TestCase):
    def test_native_transport_pins_address_tls_hostname_and_refuses_redirects(self):
        response=MagicMock()
        response.status=302
        response.getheader.return_value='identity'
        connection=MagicMock()
        connection.getresponse.return_value=response
        raw=MagicMock()
        context=MagicMock()
        with patch('production.jobs.http.client.HTTPSConnection',return_value=connection),\
             patch('production.jobs.socket.socket',return_value=raw),\
             patch('production.jobs.ssl.create_default_context',return_value=context),\
             self.assertRaises(DomainError),_http('https://cdn.example/a?sig=private','cdn.example','8.8.8.8',1):
            self.fail('Redirect accepted')
        raw.connect.assert_called_once_with(('8.8.8.8',443))
        context.wrap_socket.assert_called_once_with(raw,server_hostname='cdn.example',do_handshake_on_connect=False)
        connection.close.assert_called_once()

    def test_dns_lookup_has_deadline_and_multicast_is_rejected(self):
        def slow(host):
            time.sleep(0.1)
            return ['8.8.8.8']
        reader=SafeDownloader({'cdn.example'},resolver=slow)
        with self.assertRaises(DomainError),reader.open('https://cdn.example/a',max_bytes=100,timeout=0.01):
            self.fail('Slow DNS admitted')
        reader=SafeDownloader({'cdn.example'},resolver=lambda host:['224.0.0.1'])
        with self.assertRaises(DomainError),reader.open('https://cdn.example/a',max_bytes=100,timeout=1):
            self.fail('Multicast admitted')

    def test_url_dns_redirect_and_size_guards_precede_or_bound_transport(self):
        calls=[]
        @contextmanager
        def transport(url,host,ip,timeout):
            calls.append((host,ip))
            yield Download('image/png',[b'x'*20])
        reader = SafeDownloader({'cdn.example'},resolver=lambda host:['8.8.8.8'],transport=transport)
        for url in ('http://cdn.example/a','https://evil.example/a','https://user@cdn.example/a','https://cdn.example:444/a'):
            with self.assertRaises(DomainError),reader.open(url,max_bytes=10,timeout=1):
                self.fail('URL admitted')
        self.assertEqual(calls,[])
        private = SafeDownloader({'cdn.example'},resolver=lambda host:['127.0.0.1'],transport=transport)
        with self.assertRaises(DomainError),private.open('https://cdn.example/a',max_bytes=10,timeout=1):
            self.fail('Private address admitted')
        with self.assertRaises(DomainError),reader.open('https://cdn.example/a',max_bytes=10,timeout=1) as response:
            list(response.chunks)
        self.assertEqual(calls,[('cdn.example','8.8.8.8')])


if __name__=='__main__':
    unittest.main()


class ReturnedRepairJobTests(unittest.TestCase):
    """Real MP4 publication after a durable receipt; no external dispatch in this fixture."""
    @classmethod
    def setUpClass(cls):
        cls.videos = {}
        with tempfile.TemporaryDirectory() as directory:
            for label, size, duration, audio in [('valid', '1920x1080', 5, True), ('small', '1280x720', 5, True),
                    ('ratio', '1920x1200', 5, True), ('short', '1920x1080', 1, True), ('silent', '1920x1080', 5, False)]:
                path = Path(directory) / (label + '.mp4')
                command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=red:s={size}:r=4:d={duration}']
                if audio:
                    command += ['-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-shortest', '-c:a', 'aac']
                subprocess.run([*command, '-t', str(duration), '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-threads', '1', '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=30)
                cls.videos[label] = path.read_bytes()

    def setUp(self):
        from production.gates import Gates
        from production.submissions import Submissions
        from production.tests.test_patches import PatchTests
        self.p = PatchTests()
        self.p.setUp()
        self.addCleanup(self.p.doCleanups)
        self.store, self.pid = self.p.store, self.p.pid
        self.pickup, self.failed = self.p.pickup()
        self.repair = self.p.service.apply(self.p.actor, self.pid,
            self.p.request().model_copy(update={'source_pickup':self.p.f.ref(self.pickup)}))
        self.now = 1000.0
        self.worker = self.p.f.auth.authenticate(self.p.f.auth.provision_token('worker','worker',[self.pid],300))
        self.profile = hf_era_document('video_routes')['profiles']['hf-seedance25-video-v1']
        self.p.f.config.set('video_routes', {'method_routes': {'video': 'sd25'}, 'profiles': {'sd25': self.profile}})
        self.video = self.videos['valid']
        @contextmanager
        def download(url, host, ip, timeout):
            yield Download('video/mp4', [self.video])
        submissions = Submissions(self.store, self.p.f.auth, self.p.flow,
                                  Gates(self.store, self.p.flow, self.p.projects.media))
        self.jobs = Jobs(self.store, self.p.f.auth, submissions, self.p.projects.media,
            downloader=SafeDownloader({'cdn.example'}, resolver=lambda host:['8.8.8.8'], transport=download),
            clock=lambda:self.now, lease_seconds=20)

    def ready(self, target=None, *, request_update=None, route_update=None):
        from production.auth import SYSTEM_PROJECT
        ref = lambda obj:self.p.f.ref(obj).model_dump()
        target = target or self.repair['artifact']
        rid = self.store.get_object(self.pid, self.pid)['body']['release_id']
        request = {'job_type':'seedance_2_5', 'params':{'prompt':'Rider passes wagon', 'resolution':'1080p',
            'aspect_ratio':'16:9', 'duration':5, 'generate_audio':True, 'mode':'omni_reference'}, 'references':[]}
        request['params'].update(request_update or {})
        route = {'profile_id':'sd25', 'profile_hash':content_hash(self.profile)}
        route.update(route_update or {})
        job_id = new_id('job')
        candidate = self.store.create_object(self.pid, 'candidate', {
            'target':ref(target), 'release_id':rid, 'task':'shot', 'request':request,
            'dependencies':[ref(target)]}, 'compiler_service')
        intent = self.store.create_object(self.pid, 'dispatch-intent', {
            'operation':'submit', 'candidate':ref(candidate), 'target':ref(target),
            'release_id':rid, 'task':'shot', 'method_id':'video', 'route':route, 'request':request,
            'dependencies':[ref(candidate)]}, 'submission_service')
        attempt = self.store.create_object(self.pid, 'provider-attempt', {'intent':ref(intent), 'job_id':job_id, 'request':request}, 'worker_service')
        private = self.store.create_object(SYSTEM_PROJECT, 'provider-download', {
            'project_id':self.pid, 'attempt':ref(attempt), 'url':'https://cdn.example/result.mp4'}, 'worker_service')
        receipt = self.store.create_object(self.pid, 'provider-receipt', {
            'attempt':ref(attempt), 'download_reference':ref(private), 'state':'succeeded'}, 'worker_service')
        job = self.store.create_object(self.pid, 'job', {
            'state':'submitted', 'intent':ref(intent), 'pending_result':ref(receipt), 'attempt_id':attempt['object_id'],
            'remote_job_id':'already-created', 'dependencies':[ref(intent)]}, 'worker_service', object_id=job_id)
        return self.jobs.claim(self.worker, self.pid, job['object_id'])

    def finish(self, claim):
        return self.jobs.download_result(self.worker, self.pid, self.p.f.ref(claim['job']), claim['fence'])

    def completion(self, *, resolution='1080p', draft=True):
        """A fal completion job for a draft take, with a durable succeeded receipt."""
        ref = lambda obj:self.p.f.ref(obj).model_dump()
        fal = {**self.profile, 'job_type':'fal_seedance_2_5', 'resolutions':['480p'], 'mode':'reference', 'draft':draft,
               'output_contract':{**self.profile['output_contract'], 'minimum_pixels_by_resolution':{'480p':480, '1080p':1080}}}
        self.p.f.config.set('video_routes', {'method_routes': {'video': 'sd25'},
                                                                   'profiles': {'sd25': self.profile, 'fal25': fal}})
        rid = self.store.get_object(self.pid, self.pid)['body']['release_id']
        drafted = self.finish(self.ready())  # a real finished take stands in for the 480p draft
        draft_take = self.store.get_object(self.pid, drafted['body']['result']['object_id'])
        request = {'job_type':'fal_seedance_2_5_complete', 'references':[], 'params':{'draft_id':'draft_abc',
                   'resolution':resolution, 'aspect_ratio':'16:9', 'duration':5, 'generate_audio':True}}
        job_id = new_id('job')
        intent = self.store.create_object(self.pid, 'dispatch-intent', {
            'operation':'complete-draft', 'target':ref(draft_take), 'release_id':rid, 'task':'shot', 'method_id':'video',
            'route':{'profile_id':'fal25', 'profile_hash':content_hash(fal)}, 'request':request,
            'dependencies':[ref(draft_take)]}, 'submission_service')
        attempt = self.store.create_object(self.pid, 'provider-attempt', {'intent':ref(intent), 'job_id':job_id, 'request':request}, 'worker_service')
        private = self.store.create_object(SYSTEM_PROJECT, 'provider-download', {
            'project_id':self.pid, 'attempt':ref(attempt), 'url':'https://cdn.example/result.mp4'}, 'worker_service')
        receipt = self.store.create_object(self.pid, 'provider-receipt', {'attempt':ref(attempt), 'download_reference':ref(private),
            'state':'succeeded', 'raw_receipt':{'request_id':'r', 'seed':1953829483, 'result_host':'v3b.fal.media'}}, 'worker_service')
        job = self.store.create_object(self.pid, 'job', {
            'state':'submitted', 'intent':ref(intent), 'pending_result':ref(receipt), 'attempt_id':attempt['object_id'],
            'remote_job_id':'already-created', 'dependencies':[ref(intent)]}, 'worker_service', object_id=job_id)
        return draft_take, self.jobs.claim(self.worker, self.pid, job['object_id'])

    def test_a_completion_is_the_1080p_derivative_of_its_draft_with_the_seed(self):
        draft_take, claim = self.completion()
        result = self.finish(claim)
        self.assertEqual(result['body']['state'], 'succeeded')
        media = self.store.get_object(self.pid, result['body']['result']['object_id'])
        self.assertEqual(media['body']['completes']['object_id'], draft_take['object_id'])
        self.assertEqual(media['body']['provenance']['provider_output'], {'seed': 1953829483})
        self.assertEqual(media['body']['provenance']['output_conformance']['observed']['display_height'], 1080)
        # Its origin is the draft's origin: the same card and candidate.
        origin = self.p.flow.media_origin(self.pid, self.p.f.ref(media))
        self.assertEqual(origin['authority'], self.p.flow.media_origin(self.pid, self.p.f.ref(draft_take))['authority'])
        self.assertEqual(origin['media']['object_id'], media['object_id'])

    def test_a_completion_needs_a_draft_route_and_1080p(self):
        for kwargs in ({'draft': False}, {'resolution': '720p'}):
            with self.subTest(**kwargs):
                _, claim = self.completion(**kwargs)
                with self.assertRaises(DomainError):
                    self.finish(claim)

    def test_download_mandatorily_links_exact_bound_repair_and_preserves_failed_output(self):
        from production.patches import link_generated_repairs
        claim = self.ready()
        result = self.finish(claim)
        self.assertEqual(result['body']['state'], 'succeeded')
        lineages = self.store.list_objects(self.pid, kind='repair-lineage')
        self.assertEqual(len(lineages), 1)
        body = lineages[0]['body']
        self.assertEqual(body['repair'], self.p.f.ref(self.repair['repair']).model_dump())
        self.assertEqual(body['source_pickup'], self.p.f.ref(self.pickup).model_dump())
        self.assertEqual(body['failed_take'], self.p.f.ref(self.failed).model_dump())
        self.assertEqual(body['returned_take'], result['body']['result'])
        self.assertFalse(body['accepted'])
        self.assertEqual(self.store.get_object(self.pid, self.pickup['object_id']), self.pickup)
        self.assertEqual(self.store.get_object(self.pid, self.failed['object_id']), self.failed)
        with self.store.transaction() as conn:
            replay = link_generated_repairs(self.store, self.p.flow, self.pid,
                self.p.f.ref(self.store.get_object(self.pid, result['body']['result']['object_id'], conn=conn)), conn=conn)
        self.assertEqual(replay, lineages)
        self.assertFalse(self.p.flow.pinned_graph(self.pid, self.p.f.ref(lineages[0]))['stale'])

    def test_real_video_resolution_ratio_duration_and_audio_rejections_publish_no_take(self):
        cases = [('small', 'minimum_edge_mismatch'), ('ratio', 'aspect_ratio_mismatch'),
                 ('short', 'duration_mismatch'), ('silent', 'missing_requested_audio')]
        before = self.store.list_objects(self.pid, kind='media')
        for label, code in cases:
            with self.subTest(label=label):
                self.video = self.videos[label]
                result = self.finish(self.ready())
                self.assertEqual(result['body']['state'], 'failed')
                self.assertIn(code, [reason['code'] for reason in result['body']['last_error']['reasons']])
                self.assertIsNone(result['body']['result'])
        self.assertEqual(self.store.list_objects(self.pid, kind='media'), before)
        self.assertEqual(self.store.list_objects(self.pid, kind='repair-lineage'), [])
        self.assertEqual(len(self.store.list_objects(SYSTEM_PROJECT, kind='output-evidence')), 4)

    def test_exact_released_profile_and_effective_audio_are_required_before_download(self):
        for route, params in [({'profile_hash':'0'*64}, {}), ({'profile_id':'absent'}, {}),
                              ({}, {'generate_audio':None}), ({}, {'resolution':'720p'})]:
            with self.subTest(route=route, params=params):
                claim = self.ready(route_update=route, request_update=params)
                with patch.object(self.jobs.downloader, 'open', side_effect=AssertionError('No download before frozen authority')), self.assertRaises(DomainError) as error:
                    self.finish(claim)
                self.assertEqual(error.exception.code, 'invalid_input' if params.get('generate_audio', True) is None else 'unsupported_route')
                # Nothing downloaded; the attempt counts toward the limit, so a check that keeps failing ends in
                # quarantine instead of a loop.
                body = self.store.get_object(self.pid, claim['job']['object_id'])['body']
                self.assertEqual(body, {**claim['job']['body'], 'download_count': claim['job']['body'].get('download_count', 0) + 1})
        self.assertEqual(self.store.list_objects(SYSTEM_PROJECT, kind='output-evidence'), [])

    def test_successful_video_provenance_measures_real_stream_not_provider_echo(self):
        result = self.finish(self.ready())
        media = self.store.get_object(self.pid, result['body']['result']['object_id'])
        conformance = media['body']['provenance']['output_conformance']
        self.assertTrue(conformance['passed'])
        self.assertEqual(conformance['observed']['display_height'], 1080)
        self.assertEqual(conformance['observed']['video_duration'], 5)
        self.assertTrue(conformance['observed']['has_audio'])
        self.assertFalse(conformance['native_resolution_verified'])

    def test_failed_lineage_publication_rolls_back_media_and_completion_then_download_retries(self):
        claim = self.ready()
        before = self.store.list_objects(self.pid, kind='media')
        create = self.store.create_object
        def fail(*args, **kwargs):
            if args[1] == 'repair-lineage':
                raise DomainError('revision_conflict', 'Injected atomic publication failure')
            return create(*args, **kwargs)
        with patch.object(self.store, 'create_object', side_effect=fail), self.assertRaises(DomainError):
            self.finish(claim)
        job = self.store.get_object(self.pid, claim['job']['object_id'])
        self.assertEqual(job['body']['state'], 'unknown')
        self.assertNotIn('result', job['body'])
        self.assertEqual(self.store.list_objects(self.pid, kind='media'), before)
        self.assertEqual(self.store.list_objects(self.pid, kind='repair-lineage'), [])
        retry = self.jobs.claim(self.worker, self.pid, job['object_id'])
        self.assertEqual(retry['action'], 'download')
        self.assertEqual(self.finish(retry)['body']['state'], 'succeeded')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='repair-lineage')), 1)

    def test_unmatched_return_and_unbound_repair_do_not_guess_a_pickup(self):
        unrelated = self.p.draft('shot', 'shots/unrelated.json', self.p.card)
        self.assertEqual(self.finish(self.ready(unrelated))['body']['state'], 'succeeded')
        unbound = self.p.service.apply(self.p.actor, self.pid,
            self.p.request(target=self.repair['artifact'], key='unbound', value='Rider exits camera right'))
        self.assertEqual(self.finish(self.ready(unbound['artifact']))['body']['state'], 'succeeded')
        self.assertEqual(self.store.list_objects(self.pid, kind='repair-lineage'), [])
        self.assertEqual(self.store.get_object(self.pid, self.pickup['object_id'])['body']['status'], 'unresolved')


if __name__ == '__main__':
    unittest.main()
