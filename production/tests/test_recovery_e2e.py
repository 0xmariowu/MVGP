"""Recovery across HTTP, actual durable services and disconnected provider transports.

Process death is exercised in a child process; other named interruption points
raise SystemExit, which the worker's ordinary error handling must not swallow.
Synthetic review answers prove protocol/recovery, never artistic competence.
"""
import multiprocessing
import os
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch


from production.auth import AuthService
from production.contracts import DomainError
from production.cuts import Cuts
from production.gates import Gates
from production.jobs import Download, Jobs, SafeDownloader
from production.media import MediaStore
from production.operations import (
    BackupRequest,
    Operations,
    OperatorConfig,
    RestoreRequest,
)
from production.reader import Reader
from production.store import Store
from production.submissions import Submissions
from production.tests import test_boundary_e2e as boundary
from production.tests.test_boundary_e2e import ref
from production.worker import Worker
from production.workflow import Workflow


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.b = boundary.BoundaryTests(); self.b.setUp(); self.addCleanup(self.b.doCleanups)
        self.c, self.pid = self.b.c, self.b.pid
        self.store = self.c.store
        self.now = time.time()
        self.c.jobs.clock = lambda: self.now
        self.calls = []
        self.status = 'completed'
        self.native_hook = None
        self.download_hook = None
        self.c.worker.provider.transport = self.native
        self.c.jobs.downloader = SafeDownloader({'cdn.example'}, resolver=lambda host: ['8.8.8.8'], transport=self.download)
        uploaded = self.b.upload_video()
        self.source_raw = self.c.media.read(self.pid, uploaded['object_id'])
        output = self.c.root/'recovered-output.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=1920x1080:r=24:d=4',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
            '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(output)],
            check=True, capture_output=True, timeout=30)
        self.raw = output.read_bytes()
        self.source = uploaded
        self.request = None
        self.download_calls = 0

    def native(self, action, request):
        self.calls.append(action)
        if self.native_hook:
            self.native_hook(action)
        return {'id': 'durable-remote-id', 'status': self.status, 'result_url': 'https://cdn.example/recovered.mp4'}

    @contextmanager
    def download(self, url, host, ip, timeout):
        self.download_calls += 1
        def chunks():
            yield self.raw[:32]
            if self.download_hook:
                self.download_hook()
            yield self.raw[32:]
        yield Download('video/mp4', chunks())

    def queued_generation(self):
        candidate, shot = self.b.candidate()
        self.request = self.b.record(candidate)['body']['request']
        job = self.b.record(self.b.post('/submissions', self.b.submit_body(candidate), 202))
        return candidate, shot, job

    def restart(self):
        """Fresh authority graph reads only durable state; no cached task/receipt reuse."""
        store = Store(self.store.path)
        auth = AuthService(store, 'https://craft.example')
        worker = auth.authenticate(auth.provision_token('restarted_worker', 'worker', [self.pid], 3600))
        media = MediaStore(store, self.c.media.root)
        flow = Workflow(store, auth, self.c.config)
        gates = Gates(store, flow, media)
        submissions = Submissions(store, auth, flow, gates)
        jobs = Jobs(store, auth, submissions, media, downloader=self.c.jobs.downloader, clock=lambda: self.now)
        reader = Reader(jobs, self.c.config, transport=self.c.worker.reader.transport)
        return Worker(jobs, self.c.worker.provider, worker, [self.pid],
                      cuts=Cuts(store, auth, flow, media), reader=reader)

    def current(self, job):
        return self.store.get_object(self.pid, job['object_id'])

    def assert_unknown_hold(self, job, amount=1):
        current = self.current(job)
        self.assertEqual(current['body']['state'], 'unknown')
        self.assertNotIn('result', current['body'])
        self.assertGreaterEqual(self.store.budget(self.pid)['reserved'], amount)
        return current

    def test_death_before_dispatch_is_safe_to_retry_after_new_service_claim(self):
        _, _, job = self.queued_generation()
        with patch.object(self.c.jobs, 'begin_dispatch', side_effect=SystemExit('death before paid intent')), self.assertRaises(SystemExit):
            self.c.worker.run_once()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.list_objects(self.pid, kind='provider-attempt'), [])
        self.now += self.c.jobs.lease_seconds + 1
        restarted = self.restart()
        self.assertEqual(restarted.run_once()[0]['state'], 'succeeded')
        self.assertEqual(self.calls, ['create'])
        self.assertEqual(self.c.media.read(self.pid, self.current(job)['body']['result']['object_id']), self.raw)

    def test_process_exit_after_remote_acceptance_survives_private_restore_without_retry(self):
        _, _, job = self.queued_generation()
        accepted = self.c.root/'accepted-evidence.txt'
        def remote_accept(action):
            accepted.write_text('accepted once')
            os._exit(73)
        self.native_hook = remote_accept
        process = multiprocessing.get_context('fork').Process(target=self.c.worker.run_once)
        process.start(); process.join(timeout=20)
        if process.is_alive():
            process.terminate(); process.join(timeout=5)
            self.fail('Synthetic killed worker did not terminate')
        self.assertEqual(process.exitcode, 73)
        self.assertEqual(accepted.read_text(), 'accepted once')
        self.assertEqual(self.current(job)['body']['state'], 'dispatching')
        self.native_hook = None
        self.now += self.c.jobs.lease_seconds + 1
        self.assertEqual(self.restart().run_once()[0]['state'], 'unknown')
        unknown = self.assert_unknown_hold(job)
        # No follow-up at all: the adapter cannot list, so the operator settles it.
        self.assertEqual([o['action'] for o in self.restart().run_once()], [])
        self.assertEqual([c for c in self.calls if c != 'list'], [])  # Parent adapter never repeats the child effect.
        credentials = self.c.root/'operator-credentials'; credentials.mkdir(mode=0o700)
        operations = Operations(OperatorConfig(database=str(self.store.path.resolve()), public_origin='https://craft.example',
            operator_id='recovery_operator', credential_directory=str(credentials.resolve())))
        backup = operations.backup(BackupRequest(destination=str((self.c.root/'backup').resolve()), media_root=str(self.c.media.root.resolve())))
        restored = operations.restore(RestoreRequest(source=backup['destination'], destination=str((self.c.root/'restored').resolve()),
                                                    manifest_sha256=backup['manifest_sha256']))
        recovered = Store(restored['database'])
        self.assertEqual(recovered.get_object(self.pid, job['object_id']), unknown)
        for revision in range(1, unknown['revision']+1):
            self.assertEqual(recovered.get_object(self.pid, job['object_id'], revision=revision),
                             self.store.get_object(self.pid, job['object_id'], revision=revision))
        self.assertEqual(recovered.budget(self.pid), self.store.budget(self.pid))
        self.assertEqual(MediaStore(recovered, restored['media_root']).read(self.pid, self.source['object_id']), self.source_raw)
        with self.assertRaises(DomainError): AuthService(recovered, 'https://craft.example').authenticate(self.b.fixture.token)
        self.c.auth.authenticate(self.b.fixture.token)
        self.assertFalse(restored['active']); self.assertFalse(restored['switch_over_performed'])
        self.assertNotEqual(recovered.path, self.store.path)

    def test_response_received_but_local_receipt_lost_stays_unknown_without_blind_retry(self):
        _, _, job = self.queued_generation()
        with patch.object(self.c.jobs, 'record_receipt', side_effect=SystemExit('after remote response, before SQLite')), self.assertRaises(SystemExit):
            self.c.worker.run_once()
        self.assertEqual(self.calls, ['create'])
        self.assertEqual(self.store.list_objects(self.pid, kind='provider-receipt'), [])
        self.now += self.c.jobs.lease_seconds + 1
        self.assertEqual(self.restart().run_once()[0]['state'], 'unknown')
        self.assert_unknown_hold(job)
        # No follow-up at all: the adapter cannot list, so the operator settles it.
        self.assertEqual([o['action'] for o in self.restart().run_once()], [])
        self.assertEqual([c for c in self.calls if c != 'list'], ['create'])

    def test_known_remote_polls_then_interrupted_download_resumes_only_exact_receipt(self):
        _, _, job = self.queued_generation()
        self.status = 'provider-processing'
        self.assertEqual(self.c.worker.run_once()[0]['state'], 'unknown')
        self.assertEqual(self.current(job)['body']['remote_job_id'], 'durable-remote-id')
        self.now += 400; self.status = 'completed'
        self.download_hook = lambda: (_ for _ in ()).throw(SystemExit('stream died'))
        with self.assertRaises(SystemExit): self.restart().run_once()
        self.assertEqual(self.calls, ['create', 'get'])
        self.assertNotIn('result', self.current(job)['body'])
        pending = self.current(job)['body']['pending_result']
        self.assertTrue(pending)
        self.download_hook = None; self.now += self.c.jobs.lease_seconds + 1
        restarted = self.restart()
        self.assertEqual(restarted.run_once()[0]['state'], 'succeeded')
        self.assertEqual(self.calls, ['create', 'get'])
        self.assertEqual(self.download_calls, 2)
        media = self.current(job)['body']['result']
        self.assertEqual(self.c.media.read(self.pid, media['object_id']), self.raw)
        self.assertEqual(self.store.get_object(self.pid, media['object_id'])['body']['provenance']['provider_receipt'], pending)
        self.assertEqual(restarted.run_once(), [])
        # A complete media file does not invent a confirmed supplier charge.
        self.assertEqual(self.store.budget(self.pid)['reserved'], 1)

    def test_observer_expiry_while_original_worker_alive_quarantines_late_response(self):
        queued = self.b.post('/observations', {'idempotency_key': 'read', 'expected_revision': 1,
            'media_id': self.source['object_id'], 'reader': 'video', 'questions': ['What changes?']}, 202)
        job = self.b.record(queued)
        entered, finish = threading.Event(), threading.Event()
        calls = []
        def observer(*args):
            calls.append(1); entered.set()
            if not finish.wait(timeout=20): raise RuntimeError('test release not signaled')
            raise TimeoutError('Accepted request response lost')
        self.c.worker.reader.transport = observer
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.c.worker.run_once)
            try:
                self.assertTrue(entered.wait(timeout=10))
                self.now += self.c.jobs.lease_seconds + 1
                self.assertEqual(self.restart().run_once()[0]['state'], 'unknown')
                unknown = self.assert_unknown_hold(job)
            finally: finish.set()
            future.result(timeout=10)
        self.assertEqual(self.current(job), unknown)
        self.assertEqual(self.store.list_objects(self.pid, kind='observation'), [])
        self.assertEqual(self.restart().run_once(), [])
        self.assertEqual(calls, [1])

    def test_local_render_interruption_recovers_same_cut_without_paid_reservation(self):
        # Uploaded footage is operator-imported rough material, never a trusted final.
        item = self.store.get_object(self.pid, self.source['object_id'])
        item = self.store.append_revision(self.pid, item['object_id'], 1,
            {**item['body'], 'import_status': 'imported-unverified'}, 'importer_service')
        cut = self.b.post('/cuts', {'idempotency_key': 'rough', 'expected_revision': 1,
            'segments': [{'take': ref(item).model_dump(), 'start_seconds': 0, 'end_seconds': 0.25}],
            'intent': 'Recovery fixture rough edit.'})
        job = self.b.record(self.b.post('/cuts/render', {'idempotency_key': 'render', 'expected_revision': 1,
            'cut': cut['object_ref']}, 202))
        original = subprocess.run; encoded = []
        def interrupt(args, **kwargs):
            result = original(args, **kwargs)
            if '-max_alloc' in args:
                encoded.append(args[-1]); raise SystemExit('death after intermediate encode')
            return result
        with patch('production.cuts.subprocess.run', side_effect=interrupt), self.assertRaises(SystemExit):
            self.c.worker.run_once()
        self.assertTrue(encoded)
        self.assertNotIn('result', self.current(job)['body'])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)
        self.now += self.c.jobs.lease_seconds + 1
        restarted = self.restart()
        self.assertEqual(restarted.run_once()[0]['state'], 'succeeded')
        result = self.store.get_object(self.pid, self.current(job)['body']['result']['object_id'])
        self.assertEqual(result['body']['probe']['width'], 1920)
        self.assertEqual(result['body']['probe']['height'], 1080)
        self.assertEqual(result['body']['source_cut'], cut['object_ref'])
        self.assertEqual(self.store.list_objects(self.pid, kind='human-receipt'), [])
        self.assertEqual(self.store.budget(self.pid)['spent'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(restarted.run_once(), [])
