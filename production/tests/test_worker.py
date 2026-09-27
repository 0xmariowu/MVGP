"""Restarted stores execute only authorized generation, never fake completion."""
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


from production import review_http
from production.auth import SYSTEM_PROJECT, AuthService
from production.contracts import (
    DomainError,
    ObserveRequest,
    RenderCutRequest,
)
from production.cuts import Cuts
from production.gates import Gates
from production.jobs import Download, Jobs
from production.media import MediaStore
from production.provider_hf import (
    CommandResult,
    ExecutablePin,
    HFProvider,
    _native,
)
from production.reader import Reader
from production.store import Store
from production.submissions import Submissions
from production.tests import (
    test_cuts,
    test_jobs,
    test_reader,
)
from production.tests.fixtures import FakeProvider
from production.worker import Worker, build_worker, main
from production.workflow import Workflow


@contextmanager
def unconfirmed_model_child_exit():
    """Run real owned children, then simulate missing OS exit confirmation.

    The saved wait first reaps the killed fixture, so the deliberate error cannot
    leak a child. Only our exact test argv is intercepted; ffmpeg remains real.
    """
    temporary = tempfile.TemporaryDirectory()
    marker = Path(temporary.name) / 'ready'
    argv = [sys.executable, '-c', 'import pathlib,sys,time; sys.stdout.write("partial-model-response"); sys.stdout.flush(); pathlib.Path(sys.argv[1]).touch(); time.sleep(30)', str(marker)]
    original = subprocess.Popen
    children = []
    def start(args, *positional, **kwargs):
        child = original(args, *positional, **kwargs)
        if list(args) == argv:
            wait = child.wait
            children.append((child, wait))
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.01)
            if not marker.exists():
                raise AssertionError('Owned model fixture did not acknowledge startup')
            def unconfirmed(timeout=None):
                wait(timeout=timeout)
                raise subprocess.TimeoutExpired('owned-model-fixture', timeout)
            child.wait = unconfirmed
        return child
    with patch('production.review_http._POISONED', False), patch('subprocess.Popen', side_effect=start):
        try:
            yield argv, children
        finally:
            for child, wait in children:
                if child.returncode is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    wait(timeout=2)
            temporary.cleanup()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.j = test_jobs.JobTests()
        self.j.setUp()
        self.addCleanup(self.j.doCleanups)
        self.f,self.store,self.pid = self.j.f,self.j.store,self.j.pid
        self.root = self.f.root
        self.calls=[]
        self.remote_status='completed'
        self.transport_timeout=False
        self.on_create=None
        self.command_response=None
        def transport(action,request):
            self.calls.append(action)
            if action=='create':
                # The call can acquire a write transaction: dispatch holds none.
                with self.store.transaction() as conn:
                    self.store.append_event(self.pid,'test.network_outside_transaction',{},conn=conn)
                if self.on_create:
                    self.on_create()
                if self.transport_timeout:
                    raise TimeoutError()
            if isinstance(self.command_response, Exception):
                raise self.command_response
            if self.command_response is not None:
                return self.command_response
            return {'id':'remote1','status':self.remote_status,'result_url':'https://cdn.example/result.png?token=private'}
        self.provider=FakeProvider({'nano_banana_pro'},transport)
        self.worker=Worker(self.j.jobs,self.provider,self.j.worker,[self.pid])

    def test_quarantined_jobs_are_not_selected_or_claimed_across_cycles(self):
        for fields in ({'pending_result': {'object_id': 'receipt'}, 'download_count': self.j.jobs.max_downloads},
                       {'remote_job_id': 'remote1', 'poll_count': self.j.jobs.max_polls}):
            with self.subTest(fields=fields):
                current = self.store.get_object(self.pid, self.j.job['object_id'])
                job = self.store.append_revision(self.pid, current['object_id'], current['revision'],
                    {**self.j.job['body'], 'state': 'unknown', 'lease': None, **fields}, 'worker_service')
                events = self.store.events(self.pid)
                with patch.object(self.j.jobs, 'claim', wraps=self.j.jobs.claim) as claim:
                    for _ in range(2):
                        self.assertEqual(self.worker.run_once(), [])
                        self.assertEqual(self.store.get_object(self.pid, job['object_id']), job)
                        self.assertEqual(self.store.events(self.pid), events)
                    claim.assert_not_called()
                self.assertEqual(self.calls, [])

    def test_unknown_job_with_download_allowance_still_runs(self):
        dispatch = self.j.dispatched()
        received = self.j.jobs.record_receipt(self.j.worker, self.pid, self.f.ref(dispatch['job']),
            dispatch['fence'], self.j.receipt())
        self.store.append_revision(self.pid, received['object_id'], received['revision'],
            {**received['body'], 'state': 'unknown', 'lease': None, 'download_count': self.j.jobs.max_downloads - 1}, 'worker_service')
        outcome = self.worker.run_once()
        self.assertEqual(len(outcome), 1)
        self.assertEqual(outcome[0]['action'], 'download')
        self.assertEqual(outcome[0]['state'], 'succeeded')
        self.assertEqual(self.calls, [])

    def test_queue_discovery_uses_one_readonly_snapshot_closed_before_execution(self):
        opened = []
        active = []
        original = self.store.transaction
        @contextmanager
        def tracked(*, write=True):
            opened.append(write)
            with original(write=write) as conn:
                active.append(conn)
                try:
                    yield conn
                finally:
                    active.remove(conn)
        def selected(pid, job):
            self.assertEqual(active, [])
            self.assertEqual(pid, self.pid)
            self.assertEqual(job['object_id'], self.j.job['object_id'])
            return {'state': 'selected'}
        with patch.object(self.store, 'transaction', tracked), patch.object(self.worker, '_process', selected):
            self.assertEqual(self.worker.run_once(), [{'state': 'selected'}])
        self.assertEqual(opened, [False])
        self.assertEqual(self.calls, [])

    def test_explicit_live_switch_reaches_claim_without_overruling_release_denial(self):
        # No native call: isolate the worker boundary, then let the claim deny.
        intent = json.loads(json.dumps(self.worker._intent(self.pid, self.j.job)))
        intent['body']['cost']['mode'] = 'live'
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.provider.fake = False
                self.provider.live_enabled = enabled
                with patch.object(self.worker, '_intent', return_value=intent), patch.object(
                        self.j.jobs, 'claim', side_effect=DomainError('release_mismatch', 'Unreleased test')) as claim:
                    result = self.worker._process(self.pid, self.j.job)
                self.assertEqual(claim.call_count, int(enabled))
                self.assertEqual(result['error_code'], 'release_mismatch' if enabled else 'unsupported_route')
                self.assertEqual(result['state'], 'queued')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.list_objects(self.pid, kind='provider-attempt'), [])

    def creates(self):
        return [a for a in self.calls if a=='create']

    def test_complete_generation_persists_verified_media_without_secret_output(self):
        outcomes=self.worker.run_once()
        self.assertEqual(len(self.creates()),1)
        self.assertEqual(outcomes[0]['state'],'succeeded')
        self.assertNotIn('private',str(outcomes))
        job=self.store.get_object(self.pid,self.j.job['object_id'])
        self.assertEqual(self.store.get_object(self.pid,job['body']['result']['object_id'])['body']['probe']['width'],2048)
        self.assertEqual(self.worker.run_once(),[])

    def test_a_finished_image_becomes_its_assets_one_image_without_an_agent_call(self):
        # HF's element is a descriptor plus one image; the service binds the result.
        asset = self.j.s.target
        outcomes = self.worker.run_once()
        self.assertEqual(outcomes[0]['state'], 'succeeded')
        job = self.store.get_object(self.pid, self.j.job['object_id'])
        bound = self.store.get_object(self.pid, asset['object_id'])
        self.assertEqual(bound['revision'], asset['revision'] + 1)
        self.assertEqual(bound['author'], 'worker_service')
        self.assertEqual(bound['body']['content']['media_refs'], [job['body']['result']])
        self.assertEqual({k: v for k, v in bound['body']['content'].items() if k != 'media_refs'},
                         {k: v for k, v in asset['body']['content'].items() if k != 'media_refs'})
        self.assertIn(job['body']['result'], bound['body']['dependencies'])

    def test_an_asset_changed_after_preparing_keeps_its_images(self):
        asset = self.j.s.target
        changed = self.store.append_revision(self.pid, asset['object_id'], asset['revision'], asset['body'], 'author')
        self.worker.run_once()
        self.assertEqual(self.store.get_object(self.pid, asset['object_id'])['revision'], changed['revision'])

    def test_the_router_sends_a_job_to_its_own_adapter(self):
        # one adapter per job type; the router is transparent for the job it owns.
        from production.provider_router import ProviderRouter
        router=ProviderRouter({'nano_banana_pro':self.provider})
        worker=Worker(self.j.jobs,router,self.j.worker,[self.pid])
        outcomes=worker.run_once()
        self.assertEqual((len(self.creates()),outcomes[0]['state']),(1,'succeeded'))
        other=ProviderRouter({'seedance_2_5':self.provider})
        with self.assertRaises(DomainError):
            other._parameters({'job_type':'nano_banana_pro','params':{}},has_references=False)

    def test_completed_provider_with_small_output_is_failed_without_automatic_retry(self):
        import io

        from PIL import Image

        from production.queries import Queries
        output = io.BytesIO()
        Image.new('RGB', (12, 8), 'red').save(output, 'PNG')
        self.j.png = output.getvalue()
        before = self.store.budget(self.pid)
        self.assertEqual(self.worker.run_once()[0]['state'], 'failed')
        view = Queries(self.store, self.f.auth, self.f.flow).artifact(self.j.s.actor, self.pid, self.j.job['object_id'])
        self.assertEqual(view['details']['last_error']['code'], 'delivered_output_nonconforming')
        self.assertIn('minimum_edge_mismatch', str(view['details']['last_error']['reasons']))
        self.assertIsNone(view['details']['result'])
        self.assertNotIn('token=private', json.dumps(view))
        self.assertEqual(self.store.budget(self.pid), before)
        self.assertEqual(self.worker.run_once(), [])
        self.assertEqual(len(self.creates()), 1)

    def test_poisoned_review_http_blocks_all_worker_operations_before_dispatch(self):
        from production.review_http import FatalWorkerError
        before = self.store.get_object(self.pid, self.j.job['object_id'])
        with patch('production.review_http._POISONED', True):
            with self.assertRaises(FatalWorkerError):
                self.worker.run_once()
            self.assertFalse(self.worker.configuration_status['review_http_healthy'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.get_object(self.pid, self.j.job['object_id']), before)

    def test_adapter_mismatch_has_zero_provider_calls(self):
        # Reviews are advice; only the adapter decides here.
        self.provider.fake=False
        outcome=self.worker.run_once()[0]
        self.assertEqual(outcome['error_code'],'unsupported_route')
        self.assertEqual(self.calls,[])

    def test_post_dispatch_timeout_becomes_durable_unknown_without_retry(self):
        self.transport_timeout=True
        outcome=self.worker.run_once()[0]
        self.assertEqual(outcome['state'],'unknown')
        self.j.now+=100
        self.worker.run_once()
        self.assertEqual(len(self.creates()),1)
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_restart_with_new_store_polls_known_id_instead_of_resubmitting(self):
        self.remote_status='provider-processing'
        self.assertEqual(self.worker.run_once()[0]['state'],'unknown')
        self.j.now+=5
        restarted=Store(self.store.path)
        auth=AuthService(restarted,'https://studio.example')
        token=auth.provision_token('restarted_worker','worker',[self.pid],300)
        principal=auth.authenticate(token)
        media=MediaStore(restarted,self.f.media.root)
        flow=Workflow(restarted,auth,self.f.flow.config)
        submissions=Submissions(restarted,auth,flow,Gates(restarted,flow,media),review_check=self.j.s.review)
        jobs=Jobs(restarted,auth,submissions,media,downloader=self.j.downloader,clock=lambda:self.j.now,lease_seconds=20)
        worker=Worker(jobs,self.provider,principal,[self.pid])
        self.remote_status='completed'
        self.assertEqual(worker.run_once()[0]['state'],'succeeded')
        self.assertEqual(len(self.creates()),1)
        self.assertEqual(self.calls.count('get'),1)

    def test_cancel_during_network_refreshes_only_same_fence_for_receipt(self):
        def cancel():
            job=self.store.get_object(self.pid,self.j.job['object_id'])
            self.j.jobs.cancel(self.j.other,self.pid,self.f.ref(job))
        self.on_create=cancel
        self.assertEqual(self.worker.run_once()[0]['state'],'succeeded')
        self.assertTrue(self.store.get_object(self.pid,self.j.job['object_id'])['body']['cancel_requested'])
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-receipt')),1)

    def test_changed_lease_cannot_be_refreshed_into_other_workers_authority(self):
        def replace():
            self.j.now+=21
            self.j.jobs.claim(self.j.other,self.pid,self.j.job['object_id'])
        self.on_create=replace
        result=self.worker.run_once()[0]
        self.assertEqual(result['error_code'],'revision_conflict')
        self.assertEqual(self.store.list_objects(self.pid,kind='provider-receipt'),[])

    def test_invalid_result_is_not_false_success_and_unsupported_observation_stays_queued(self):
        @contextmanager
        def bad(url,host,ip,timeout):
            yield Download('image/png',[b'not an image'])
        self.j.downloader.transport=bad
        result=self.worker.run_once()[0]
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['error_code'],'invalid_media')
        image=self.f.image()
        observed=self.j.s.service.observe(self.j.s.actor,self.pid,ObserveRequest(idempotency_key='observe-worker',
            expected_revision=1,media_id=image['object_id'],questions=['What is visible?'],reader='image'))
        self.worker.run_once()
        self.assertEqual(self.store.get_object(self.pid,observed['object_id'])['body']['state'],'queued')

    def test_loop_stop_and_configuration_bounds(self):
        stop=threading.Event()
        stop.set()
        self.assertEqual(self.worker.run(stop,max_iterations=3),[])
        self.assertEqual(self.calls,[])
        with self.assertRaises(ValueError):
            Worker(self.j.jobs,self.provider,self.j.worker,[self.pid],concurrency=13)
        worker=Worker(self.j.jobs,self.provider,self.j.worker,[self.pid],concurrency=8)
        self.assertEqual(worker.concurrency,8)
        self.provider.timeout=20
        with self.assertRaises(ValueError):
            Worker(self.j.jobs,self.provider,self.j.worker,[self.pid])

    def test_worker_with_no_list_follows_its_all_projects_scope(self):
        # a new project is picked up without a config change or restart.
        auth=self.j.jobs.auth
        principal=auth.authenticate(auth.provision_token('worker_all','worker',[],300,all_projects=True))
        worker=Worker(self.j.jobs,self.provider,principal,[])
        self.assertIn(self.pid,worker.projects)
        self.store.create_project('project_fresh',{'title':'New film'},'maker')
        self.assertNotIn('project_fresh',worker.projects)
        worker._scope_due=0.0
        worker.run_once()
        self.assertIn('project_fresh',worker.projects)
        pinned=Worker(self.j.jobs,self.provider,principal,[self.pid])
        pinned.run_once()
        self.assertEqual(pinned.projects,(self.pid,))

    def test_bootstrap_refuses_public_config_symlink_and_dynamic_factory(self):
        config=self.root/'service.json'
        config.write_text('{"factory":"untrusted.module", "api_key":"must-not-print"}')
        config.chmod(0o644)
        with self.assertRaises(DomainError) as error:
            build_worker(config)
        self.assertNotIn('must-not-print',str(error.exception))
        config.chmod(0o600)
        with self.assertRaises(DomainError) as error:
            build_worker(config)
        self.assertNotIn('must-not-print',str(error.exception))
        link=self.root/'linked.json'
        link.symlink_to(config)
        with self.assertRaises(DomainError):
            build_worker(link)

    def test_stale_input_blocks_before_provider_or_dispatch_journal(self):
        self.store.append_revision(self.pid,self.j.s.target['object_id'],1,self.j.s.target['body'],'author')
        before=self.store.budget(self.pid)
        result=self.worker.run_once()[0]
        self.assertEqual(result['error_code'],'rule_violation')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.list_objects(self.pid,kind='provider-attempt'),[])
        # Bug hunt 2026-09-25: never sent, so it is cancelled and its hold released at zero, not retried forever.
        job=self.store.get_object(self.pid,self.j.job['object_id'])
        self.assertEqual((job['body']['state'],job['body']['last_error']['code']),('cancelled','rule_violation'))
        after=self.store.budget(self.pid)
        self.assertEqual((after['reserved'],after['spent']),(before['reserved']-10,before['spent']))
        self.assertEqual(self.worker.run_once(),[])

    def test_named_bootstrap_uses_real_services_and_no_review_override(self):
        token=self.f.auth.provision_token('bootstrap_worker','worker',[self.pid],300)
        config={'storage':{'mode':'development','database':str(self.store.path.resolve()),'media_root':str(self.f.media.root.resolve())},
            'public_origin':'https://studio.example',
            'project_ids':[self.pid],'worker_token_env':'MVGP_TEST_WORKER_TOKEN','download_hosts':['cdn.example'],
            'fal':{'capability_roles':{'fal_seedance_2_5':'fal_video_capability'},'timeout':10}}
        path=self.root/'private-worker.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        with patch.dict(os.environ,{'MVGP_TEST_WORKER_TOKEN':token}):
            worker=build_worker(path)
        self.addCleanup(worker.close)
        self.assertIsInstance(worker.jobs.store,Store)
        self.assertEqual(worker.configuration_status['storage']['mode'],'development')
        self.assertFalse(worker.provider.fake)
        self.assertFalse(worker.provider.live_enabled)
        self.assertIsNone(worker.jobs.submissions.review_check)
        self.assertNotIn('independent_reviews', worker.configuration_status)
        self.assertEqual(worker.run_once()[0]['error_code'],'unsupported_route')
        self.assertEqual(self.calls,[])

    def test_film_service_bootstrap_has_read_only_qualification_and_receipts_without_paid_review(self):
        # Release 71 rehearsal: the film service's final request needs both, even when paid review is off.
        from production.decisions import Decisions
        token=self.f.auth.provision_token('bootstrap_worker','worker',[self.pid],300)
        film=self.f.auth.provision_token('film_service','agent',[self.pid],300)
        path=self.bootstrap_config({'mode':'development','database':str(self.store.path.resolve()),
                                    'media_root':str(self.f.media.root.resolve())})
        config=json.loads(path.read_text())
        config.update(cuts_enabled=True,film_token_env='MVGP_TEST_FILM_TOKEN',
                      ffmpeg_path=str(Path(shutil.which('ffmpeg')).resolve()),ffprobe_path=str(Path(shutil.which('ffprobe')).resolve()))
        path.write_text(json.dumps(config))
        # This fixture release has no cut policy document; the test is about the film service's wiring.
        with patch.dict(os.environ,{'MVGP_TEST_WORKER_TOKEN':token,'MVGP_TEST_FILM_TOKEN':film}),\
                patch('production.worker.Cuts.policy', return_value={}):
            worker=build_worker(path)
        self.addCleanup(worker.close)
        decisions=worker.film.decisions
        self.assertIsInstance(decisions,Decisions)
        self.assertIsNone(decisions.receipt_check)  # the owner's confirmation is the acceptance
        self.assertEqual(worker.film.principal.actor_id,'film_service')

    def test_native_id_acknowledgement_keeps_job_for_polling_without_resubmit(self):
        native_id='00000000-0000-0000-0000-000000000099'
        self.command_response={'id':native_id,'status':'queued'}
        outcome=self.worker.run_once()[0]
        self.assertEqual(outcome['state'],'submitted')
        job=self.store.get_object(self.pid,self.j.job['object_id'])
        self.assertEqual(job['body']['remote_job_id'],native_id)
        self.assertEqual(len(self.creates()),1)
        # Before the scheduled poll there is no second create.
        self.worker.run_once()
        self.assertEqual(len(self.creates()),1)

    def bootstrap_config(self, storage):
        config={'storage':storage,'public_origin':'https://studio.example',
                'project_ids':[self.pid],'worker_token_env':'MVGP_TEST_WORKER_TOKEN'}
        path=self.root/'storage-worker.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        return path

    def test_bootstrap_requires_explicit_storage_no_legacy_or_production_fallback(self):
        legacy=self.bootstrap_config({'mode':'development','database':str(self.store.path.resolve()),
                                      'media_root':str(self.f.media.root.resolve())})
        config=json.loads(legacy.read_text())
        config.update(database=config['storage']['database'],media_root=config['storage']['media_root'])
        config.pop('storage')
        legacy.write_text(json.dumps(config))
        with self.assertRaises(DomainError):
            build_worker(legacy)
        missing=(self.root/'must-not-create.sqlite').resolve()
        path=self.bootstrap_config({'mode':'local','database':str(missing),
                                    'media_root':str((self.root/'production-media').resolve())})
        with patch.dict(os.environ,{'MVGP_TEST_WORKER_TOKEN':'present-not-authority'}), self.assertRaises(DomainError):
            build_worker(path)
        self.assertFalse(missing.exists())
        self.assertEqual(self.calls,[])

    def test_worker_main_closes_runtime_on_success_and_failure(self):
        for failure in (None,DomainError('unknown_outcome','Storage unconfirmed')):
            worker=MagicMock()
            worker.configuration_status={'storage':{'mode':'cloudflare','bound':True}}
            worker.run_once.side_effect=failure
            worker.run_once.return_value=[]
            with patch('production.worker.build_worker',return_value=worker), patch('builtins.print'):
                self.assertEqual(main(['--config','/unused','--once']),1 if failure else 0)
            worker.close.assert_called_once()

    def test_an_interrupt_during_a_paid_send_still_records_its_receipt(self):
        # an interrupt in a foreground run (Ctrl-C) lets the send in flight finish and record its
        # receipt before the loop ends. Deploy and restart use SIGTERM (a background worker ignores SIGINT).
        self.on_create=lambda: os.kill(os.getpid(),signal.SIGINT)
        with self.assertRaises(KeyboardInterrupt):
            self.worker.run_once()
        job=self.store.get_object(self.pid,self.j.job['object_id'])
        self.assertEqual(len(self.creates()),1)
        self.assertEqual(job['body']['state'],'succeeded')
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-receipt')),1)

    def test_a_stop_signal_lets_the_iteration_in_flight_finish(self):
        # SIGTERM (deploy, rollback, restart) ends the loop after the send in flight.
        seen={}
        def run(stop,poll_interval):
            os.kill(os.getpid(),signal.SIGTERM)  # arrives while a paid send is in flight
            seen['stopped_during_send']=stop.is_set()
            seen['send_finished']=True
            stop.wait(5)
        worker=MagicMock()
        worker.configuration_status={}
        worker.run.side_effect=run
        before=signal.getsignal(signal.SIGTERM)
        with patch('production.worker.build_worker',return_value=worker), patch('builtins.print'):
            self.assertEqual(main(['--config','/unused']),0)
        self.assertEqual(seen,{'stopped_during_send':True,'send_finished':True})
        worker.close.assert_called_once()
        self.assertIs(signal.getsignal(signal.SIGTERM),before)

    def test_configuration_concurrency_bounds(self):
        from pydantic import ValidationError

        from production.worker import WorkerConfiguration

        path=self.bootstrap_config({'mode':'development','database':str(self.store.path.resolve()),
                                    'media_root':str(self.f.media.root.resolve())})
        config=json.loads(path.read_text())
        for concurrency in (8,12):
            with self.subTest(concurrency=concurrency):
                self.assertEqual(WorkerConfiguration.model_validate({**config,'concurrency':concurrency}).concurrency,
                                 concurrency)
        for concurrency in (0,13):
            with self.subTest(concurrency=concurrency), self.assertRaises(ValidationError):
                WorkerConfiguration.model_validate({**config,'concurrency':concurrency})

    def test_run_once_dispatches_three_of_four_pending_jobs(self):
        self.store.set_budget(self.pid,100,'credit')
        for index in range(3):
            target=self.f.draft(self.f.definition())
            candidate=self.j.s.c.compiler.prepare(self.j.s.actor,self.pid,
                self.j.s.c.request(target,key=f'parallel-prepare-{index}'))
            self.j.s.service.submit(self.j.s.actor,self.pid,
                self.j.s.request(key=f'parallel-submit-{index}',candidate=candidate))
        pending=self.store.list_objects(self.pid,kind='job')
        self.assertEqual(len(pending),4)
        self.assertTrue(all(job['body']['state']=='queued' for job in pending))
        worker=Worker(self.j.jobs,self.provider,self.j.worker,[self.pid],concurrency=3)
        outcomes=worker.run_once()
        self.assertEqual(len(outcomes),3)
        self.assertEqual(len({outcome['job_id'] for outcome in outcomes}),3)
        self.assertTrue(all(outcome['state']=='succeeded' for outcome in outcomes))
        self.assertEqual(len(self.creates()),3)
        self.assertCountEqual([job['body']['state'] for job in self.store.list_objects(self.pid,kind='job')],
                              ['succeeded','succeeded','succeeded','queued'])

    def test_concurrent_run_once_respects_instance_concurrency(self):
        entered=threading.Event()
        release=threading.Event()
        self.on_create=lambda:(entered.set(),release.wait(2))
        outcomes=[]
        thread=threading.Thread(target=lambda:outcomes.extend(self.worker.run_once()))
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(self.worker.run_once(),[])
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(len(self.creates()),1)
        self.assertEqual(outcomes[0]['state'],'succeeded')


class HFAdapterTests(unittest.TestCase):
    """The Higgsfield CLI adapter and its listing settlement; removed with the adapter."""
    def setUp(self):
        self.j = test_jobs.JobTests()
        self.j.setUp()
        self.addCleanup(self.j.doCleanups)
        self.f,self.store,self.pid = self.j.f,self.j.store,self.j.pid
        self.root = self.f.root
        binary=self.root/'hf-native'
        binary.write_bytes(b'\xcf\xfa\xed\xfe fixture')
        binary.chmod(0o700)
        home=self.root/'service-home'
        home.mkdir(mode=0o700)
        self.pin=ExecutablePin(binary,hashlib.sha256(binary.read_bytes()).hexdigest(),'higgsfield test')
        self.caps={'nano_banana_pro':{'job_type':'nano_banana_pro','type':'image','params':[
            {'name':'prompt','type':'string','required':True},{'name':'resolution','type':'string','enum':['1k','2k','4k']},
            {'name':'aspect_ratio','type':'string','enum':['16:9','1:1']},{'name':'image_references','type':'array'}]}}
        self.calls=[]
        self.remote_status='completed'
        self.transport_timeout=False
        self.on_create=None
        self.command_response=None
        def transport(argv,env,timeout,max_bytes):
            self.calls.append(argv)
            if argv[1:]==['--version']:
                return CommandResult(0,b'higgsfield test')
            if argv[1:3]==['generate','create']:
                # The call can acquire a write transaction: dispatch holds none.
                with self.store.transaction() as conn:
                    self.store.append_event(self.pid,'test.network_outside_transaction',{},conn=conn)
                if self.on_create:
                    self.on_create()
                if self.transport_timeout:
                    raise TimeoutError()
            if isinstance(self.command_response, Exception):
                raise self.command_response
            if self.command_response is not None:
                return self.command_response
            params=self.j.s.candidate['body']['request']['params']
            return CommandResult(0,json.dumps({'id':'remote1','job_type':'nano_banana_pro','status':self.remote_status,
                'params':params,'result_url':'https://cdn.example/result.png?token=private'}).encode())
        self.provider=HFProvider(self.caps,self.pin,service_home=home,service_uid=os.getuid(),
            media_root=self.f.media.root,transport=transport,timeout=1)
        self.worker=Worker(self.j.jobs,self.provider,self.j.worker,[self.pid])

    def creates(self):
        return [a for a in self.calls if a[1:3]==['generate','create']]

    def test_actual_native_fatal_blocks_new_worker_iteration_and_keeps_attempt(self):
        self.provider._version_checked = True
        calls = []
        with unconfirmed_model_child_exit() as (argv, children):
            def transport(command, env, timeout, maximum):
                calls.append(command)
                return _native(argv, env, 0.1, maximum)
            self.provider.transport = transport
            with self.assertRaises(review_http.FatalWorkerError) as error:
                self.worker.run_once()
            self.assertTrue(error.exception.command_result.cleanup_failed)
            self.assertIn(b'partial-model-response', error.exception.command_result.stdout)
            job = self.store.get_object(self.pid, self.j.job['object_id'])
            attempt = self.store.get_object(self.pid, job['body']['attempt_id'])
            self.assertEqual(job['body']['state'], 'dispatching')
            self.assertEqual(attempt['body']['job_id'], job['object_id'])
            budget = self.store.budget(self.pid)
            self.assertGreater(budget['reserved'], 0)
            with self.assertRaises(review_http.FatalWorkerError):
                self.worker.run_once()
            self.assertEqual(self.store.get_object(self.pid, job['object_id']), job)
            self.assertEqual(self.store.get_object(self.pid, attempt['object_id']), attempt)
            self.assertEqual(self.store.budget(self.pid), budget)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0][0].returncode)

    def test_a_job_whose_provider_cannot_list_is_left_to_the_operator(self):
        # apilio has no listing, so a timed-out image is never closed as absent.
        from production.provider_router import ProviderRouter
        t,_=self.timed_out_take()
        router=ProviderRouter({'nano_banana_pro':self.provider})
        router.adapters['nano_banana_pro']=type('NoList',(),{'can_list':False,'fake':True,'timeout':1})()
        worker=Worker(self.j.jobs,router,self.j.worker,[self.pid])
        intent=worker._intent(self.pid,self.store.get_object(self.pid,self.j.job['object_id']))
        self.assertFalse(worker._listing_due(self.store.get_object(self.pid,self.j.job['object_id']),intent))

    def timed_out_take(self):
        """One create that hit the CLI timeout; returns its attempt time and prompt."""
        self.transport_timeout=True
        self.assertEqual(self.worker.run_once()[0]['state'],'unknown')
        self.transport_timeout=False
        job=self.store.get_object(self.pid,self.j.job['object_id'])
        attempt=self.store.get_object(self.pid,job['body']['attempt_id'])
        prompt=self.j.s.candidate['body']['request']['params']['prompt']
        return datetime.fromisoformat(attempt['created_at'].replace('Z','+00:00')),prompt

    def listing(self, t, items):
        old={'id':'old','created_at':(t-timedelta(seconds=90)).isoformat(),'job_type':'nano_banana_pro','params':{'prompt':'other'}}
        # Same shape as the real CLI: `generate list --json` prints a bare array.
        self.command_response=CommandResult(0,json.dumps([old,*items]).encode())

    def test_timed_out_create_closes_absent_by_itself_once_the_window_closes(self):
        # Owner 2026-09-24: no operator command and no thirty-minute wait.
        t,_=self.timed_out_take()
        self.listing(t,[])
        with patch('production.listing_reconcile.now',return_value=t+timedelta(seconds=181)):
            outcome=self.worker.run_once()[0]
        self.assertEqual(outcome['action'],'listing-absent')
        self.assertEqual(outcome['state'],'failed')
        body=self.store.get_object(self.pid,self.j.job['object_id'])['body']
        self.assertEqual(body['last_error'],'provider_absent')
        self.assertIn(['generate','list','--image','--size','50','--json'],[a[1:] for a in self.calls])
        self.assertEqual(len(self.creates()),1)
        record=self.store.list_objects(SYSTEM_PROJECT,kind='generation-provider-reconciliation')[0]
        self.assertEqual(record['author'],'worker_service')
        self.assertFalse(record['body']['billing_changed'])
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_timed_out_create_waits_while_the_window_is_open_and_looks_again_later(self):
        t,_=self.timed_out_take()
        self.listing(t,[])
        with patch('production.listing_reconcile.now',return_value=t+timedelta(seconds=130)):
            self.assertEqual(self.worker.run_once()[0]['action'],'listing-wait')
            lists=len([a for a in self.calls if a[1:3]==['generate','list']])
            self.assertEqual(self.worker.run_once(),[])
            self.assertEqual(len([a for a in self.calls if a[1:3]==['generate','list']]),lists)
        self.assertEqual(self.store.get_object(self.pid,self.j.job['object_id'])['body']['state'],'unknown')
        self.worker._listing_next.clear()
        with patch('production.listing_reconcile.now',return_value=t+timedelta(seconds=181)):
            self.assertEqual(self.worker.run_once()[0]['state'],'failed')

    def test_timed_out_create_adopts_its_listed_job_at_once(self):
        t,prompt=self.timed_out_take()
        self.listing(t,[{'id':'hf-late','created_at':(t+timedelta(seconds=40)).isoformat(),'job_type':'nano_banana_pro',
                         'params':{'prompt':prompt}}])
        with patch('production.listing_reconcile.now',return_value=t+timedelta(seconds=125)):
            outcome=self.worker.run_once()[0]
        self.assertEqual(outcome['action'],'listing-adopt')
        body=self.store.get_object(self.pid,self.j.job['object_id'])['body']
        self.assertEqual(body['remote_job_id'],'hf-late')
        self.assertEqual(body['state'],'unknown')
        self.assertEqual(len(self.creates()),1)

    def test_timed_out_create_never_adopts_a_job_another_take_owns(self):
        t,prompt=self.timed_out_take()
        # Owned by a take of another film on the same Higgsfield account.
        self.store.create_project('other_film',{},'operator')
        self.store.create_object('other_film','job',{'state':'running','remote_job_id':'hf-owned'},'worker_service')
        self.listing(t,[{'id':'hf-owned','created_at':(t+timedelta(seconds=40)).isoformat(),'job_type':'nano_banana_pro',
                         'params':{'prompt':prompt}}])
        with patch('production.listing_reconcile.now',return_value=t+timedelta(seconds=181)):
            self.assertEqual(self.worker.run_once()[0]['action'],'listing-absent')

    def test_a_store_failure_while_settling_never_stops_the_worker(self):
        t,_=self.timed_out_take()
        self.listing(t,[])
        with patch('production.listing_reconcile.now',return_value=t+timedelta(seconds=181)),\
                patch('production.listing_reconcile.owned',side_effect=sqlite3.OperationalError('database is locked')):
            outcome=self.worker.run_once()[0]
        self.assertEqual((outcome['action'],outcome['error_code']),('listing','provider_failure'))
        self.assertEqual(self.store.get_object(self.pid,self.j.job['object_id'])['body']['state'],'unknown')

    def test_unreadable_listing_leaves_the_take_unknown(self):
        t,_=self.timed_out_take()
        self.command_response=CommandResult(0,b'{"unexpected": true}')
        with patch('production.listing_reconcile.now',return_value=t+timedelta(seconds=181)):
            outcome=self.worker.run_once()[0]
        self.assertEqual(outcome['error_code'],'provider_failure')
        self.assertEqual(self.store.get_object(self.pid,self.j.job['object_id'])['body']['state'],'unknown')

    def test_named_bootstrap_uses_real_services_and_no_review_override(self):
        token=self.f.auth.provision_token('bootstrap_worker','worker',[self.pid],300)
        # the lean worker builds the Higgsfield adapter from `hf`, capabilities from runtime.json.
        config={'storage':{'mode':'development','database':str(self.store.path.resolve()),'media_root':str(self.f.media.root.resolve())},
            'public_origin':'https://studio.example',
            'project_ids':[self.pid],'worker_token_env':'MVGP_TEST_WORKER_TOKEN','download_hosts':['cdn.example'],
            'hf':{'native_path':str(self.pin.path),'sha256':self.pin.sha256,'version':self.pin.version,
                  'credential_home':str(self.provider.home),'service_uid':os.getuid(),
                  'capability_roles':{'seedance_2_5':'hf_video_capability'},'timeout':1}}
        path=self.root/'private-worker.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        with patch.dict(os.environ,{'MVGP_TEST_WORKER_TOKEN':token}):
            worker=build_worker(path)
        self.addCleanup(worker.close)
        self.assertIsInstance(worker.jobs.store,Store)
        self.assertEqual(worker.configuration_status['storage']['mode'],'development')
        self.assertIsInstance(worker.provider.adapters['seedance_2_5'],HFProvider)
        self.assertFalse(worker.provider.fake)
        self.assertFalse(worker.provider.live_enabled)
        self.assertIsNone(worker.jobs.submissions.review_check)
        self.assertNotIn('independent_reviews', worker.configuration_status)
        self.assertEqual(worker.run_once()[0]['error_code'],'unsupported_route')
        self.assertEqual(self.calls,[])



class ObservationWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_reader.ReaderTests.setUpClass()
        cls.addClassCleanup(test_reader.ReaderTests.doClassCleanups)

    def setUp(self):
        self.r = test_reader.ReaderTests()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)
        self.f,self.store,self.pid = self.r.f,self.r.store,self.r.pid
        self.jobs = self.r.jobs
        self.worker = Worker(self.jobs,None,self.r.worker,[self.pid],reader=self.r.reader)

    def enqueue(self, scale=1.0):
        return self.r.s.service.observe(self.r.s.actor,self.pid,ObserveRequest(idempotency_key='worker-observe',
            expected_revision=1,media_id=self.r.source['object_id'],reader='video',questions=['Is the image red?'],time_scale=scale))

    def restart(self):
        store = Store(self.store.path)
        auth = AuthService(store,'https://studio.example')
        actor = auth.authenticate(auth.provision_token('new_worker','worker',[self.pid],300))
        media = MediaStore(store,self.f.media.root)
        flow = Workflow(store,auth,self.f.flow.config)
        jobs = Jobs(store,auth,Submissions(store,auth,flow,Gates(store,flow,media)),media,lease_seconds=300)
        reader = Reader(jobs,self.f.flow.config,transport=self.r.transport)
        return Worker(jobs,None,actor,[self.pid],reader=reader)

    def test_restart_before_observe_and_persist_bound_result_holds_unknown_cost(self):
        job = self.enqueue()
        restarted = self.restart()
        result = restarted.run_once()[0]
        self.assertEqual(result['state'],'succeeded')
        record = self.store.get_object(self.pid,job['object_id'])
        observation = self.store.get_object(self.pid,record['body']['result']['object_id'])
        self.assertEqual(observation['author'],'reader_service')
        self.assertEqual(observation['body']['source'],self.f.ref(self.r.source).model_dump())
        self.assertFalse(observation['body']['consumption']['audio_evidence_available'])
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)
        self.assertEqual(restarted.run_once(),[])
        self.assertEqual(len(self.r.calls),1)

    def test_ambiguous_observe_not_retried_after_restart(self):
        self.enqueue()
        calls = []
        def timeout(*args):
            calls.append(1)
            raise TimeoutError()
        self.r.reader.transport = timeout
        self.assertEqual(self.worker.run_once()[0]['state'],'unknown')
        self.assertEqual(self.restart().run_once(),[])
        self.assertEqual(len(calls),1)
        observation = self.store.list_objects(self.pid,kind='observation')[0]
        self.assertEqual(observation['body']['status'],'unknown')
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_expired_dispatched_observe_is_quarantined_without_new_call(self):
        job = self.enqueue()
        self.jobs.clock = lambda:1000
        claim = self.jobs.claim(self.r.worker,self.pid,job['object_id'])
        self.jobs.begin_dispatch(self.r.worker,self.pid,self.f.ref(claim['job']),claim['fence'])
        restarted = self.restart()
        restarted.jobs.clock = lambda:1301
        self.assertEqual(restarted.run_once()[0]['state'],'unknown')
        self.assertEqual(self.r.calls,[])

    def test_failed_json_retained_without_approval_and_cancel_same_fence(self):
        job = self.enqueue()
        original = self.r.transport
        def cancel(*args):
            latest = self.store.get_object(self.pid,job['object_id'])
            self.jobs.cancel(self.r.worker,self.pid,self.f.ref(latest))
            return original(*args)
        self.r.reader.transport = cancel
        self.r.response['candidates'][0]['content']['parts'][0]['text'] = '```json {} ```'
        self.assertEqual(self.worker.run_once()[0]['state'],'failed')
        record = self.store.get_object(self.pid,job['object_id'])
        self.assertTrue(record['body']['cancel_requested'])
        self.assertEqual(self.store.list_objects(self.pid,kind='observation')[0]['body']['status'],'failed')
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_retimed_visual_derivative_is_durable_and_original_audio_remains(self):
        job = self.enqueue(scale=4.0)
        self.assertEqual(self.worker.run_once()[0]['state'],'succeeded')
        record = self.store.get_object(self.pid,job['object_id'])
        derivative = self.store.get_object(self.pid,record['body']['visual_derivative']['object_id'])
        self.assertEqual(derivative['body']['derivative_of'],self.f.ref(self.r.source).model_dump())
        self.assertFalse(derivative['body']['probe']['has_audio'])
        def packets(reference):
            path = self.f.media.path_for(self.pid, reference['object_id'])
            result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
                '-show_entries', 'packet=pts_time,data_hash', '-show_data_hash', 'sha256', '-of', 'json', str(path)],
                check=True, capture_output=True, text=True)
            return json.loads(result.stdout)['packets']
        original_packets, slow_packets = packets(self.r.source), packets(derivative)
        self.assertEqual(len(slow_packets), len(original_packets), 'No duplicated or discarded original frames')
        self.assertEqual([p['data_hash'] for p in slow_packets], [p['data_hash'] for p in original_packets])
        for before, after in zip(original_packets, slow_packets):
            self.assertAlmostEqual(float(after['pts_time']) - float(slow_packets[0]['pts_time']),
                4 * (float(before['pts_time']) - float(original_packets[0]['pts_time'])), places=4)
        observation = self.store.get_object(self.pid,record['body']['result']['object_id'])
        self.assertEqual([x['time_scale'] for x in observation['body']['inputs']],[1.0,4.0])

    def test_live_disabled_blocks_before_dispatch_or_credentials(self):
        job = self.enqueue()
        self.worker.reader = Reader(self.jobs,self.f.flow.config,credential_provider=lambda _:self.fail('No key lookup'))
        self.assertEqual(self.worker.run_once()[0]['error_code'],'forbidden')
        self.assertEqual(self.store.get_object(self.pid,job['object_id'])['body']['state'],'queued')
        self.assertEqual(self.store.list_objects(self.pid,kind='provider-attempt'),[])

    def test_observation_persistence_failure_cannot_leave_false_terminal_or_retry(self):
        self.enqueue()
        create = self.store.create_object
        def fail(pid,kind,*args,**kwargs):
            if kind=='observation':
                raise RuntimeError('Injected publication failure')
            return create(pid,kind,*args,**kwargs)
        with patch.object(self.store,'create_object',side_effect=fail):
            self.assertEqual(self.worker.run_once()[0]['state'],'unknown')
        self.assertEqual(self.store.list_objects(self.pid,kind='observation'),[])
        self.assertEqual(self.restart().run_once(),[])
        self.assertEqual(len(self.r.calls),1)

    def test_retimed_preparation_survives_crash_before_paid_dispatch(self):
        job = self.enqueue(scale=4.0)
        self.jobs.clock = lambda:1000
        with patch.object(self.jobs,'begin_dispatch',side_effect=DomainError('revision_conflict','Injected pre-call stop')):
            self.assertEqual(self.worker.run_once()[0]['state'],'queued')
        previous = self.store.get_object(self.pid,job['object_id'])['body']['visual_derivative']
        self.assertEqual(self.r.calls,[])
        restarted = self.restart()
        restarted.jobs.clock = lambda:1301
        with patch('production.worker.subprocess.run',side_effect=AssertionError('Must reuse derivative')):
            self.assertEqual(restarted.run_once()[0]['state'],'succeeded')
        self.assertEqual(self.store.get_object(self.pid,job['object_id'])['body']['visual_derivative'],previous)
        self.assertEqual(len(self.r.calls),1)

    def test_observation_stale_fence_discards_result_without_overwriting_quarantine(self):
        job = self.enqueue()
        self.jobs.clock = lambda:1000
        other = self.f.auth.authenticate(self.f.auth.provision_token('other_worker','worker',[self.pid],300))
        original = self.r.transport
        def expire(*args):
            self.jobs.clock = lambda:1301
            self.jobs.claim(other,self.pid,job['object_id'])
            return original(*args)
        self.r.reader.transport = expire
        self.assertEqual(self.worker.run_once()[0]['state'],'unknown')
        self.assertEqual(self.store.list_objects(self.pid,kind='observation'),[])
        self.assertEqual(len(self.r.calls),1)

    def test_source_frames_are_atomic_actual_and_never_model_authority(self):
        job = self.enqueue()
        original = self.r.reader.observe
        def forged(*args, **kwargs):
            result = original(*args, **kwargs)
            result['frames'] = [{'media': {'object_id': 'forged'}, 'source_seconds': 999}]
            return result
        with patch.object(self.r.reader, 'observe', side_effect=forged):
            self.assertEqual(self.worker.run_once()[0]['state'], 'succeeded')
        current = self.store.get_object(self.pid, job['object_id'])
        prepared = current['body']['prepared_frames']
        observation = self.store.get_object(self.pid, current['body']['result']['object_id'])
        self.assertEqual(len(prepared['frames']), 6)
        self.assertEqual(observation['body']['frames'], prepared['frames'])
        self.assertEqual([f['source_seconds'] for f in prepared['frames']], [0, .1, .3, .5, .7, .9])
        self.assertFalse(prepared['exhaustive'])
        for frame in prepared['frames']:
            media = self.store.get_object(self.pid, frame['media']['object_id'])
            self.assertEqual(media['author'], 'worker_service')
            self.assertEqual(media['body']['derivative_of'], self.f.ref(self.r.source).model_dump())
            self.assertTrue(self.f.media.read(self.pid, media['object_id']).startswith(b'\x89PNG'))
            self.assertIn(frame['media'], observation['body']['dependencies'])
        self.assertEqual(self.f.media.read(self.pid, self.r.source['object_id']), self.r.video)
        self.assertFalse(observation['body']['consumption']['audio_evidence_available'])

    def test_frame_failure_before_dispatch_releases_only_confirmed_uncalled_budget(self):
        job = self.enqueue()
        with patch('production.worker.FrameEvidence.extract', side_effect=DomainError('invalid_media', 'bad frames')):
            self.assertEqual(self.worker.run_once()[0]['state'], 'failed')
        current = self.store.get_object(self.pid, job['object_id'])
        self.assertEqual(current['body']['preparation_failure']['code'], 'invalid_media')
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)
        self.assertEqual(self.store.budget(self.pid)['spent'], 0)
        self.assertEqual(self.store.list_objects(self.pid, kind='provider-attempt'), [])
        self.assertEqual(self.r.calls, [])
        self.assertEqual(self.restart().run_once(), [])

    def test_frame_media_failure_rolls_back_whole_batch(self):
        job = self.enqueue()
        before = self.store.list_objects(self.pid, kind='media')
        create = self.store.create_object
        count = 0
        def fail(pid, kind, *args, **kwargs):
            nonlocal count
            if kind == 'media':
                count += 1
                if count == 3:
                    raise RuntimeError('publication crash')
            return create(pid, kind, *args, **kwargs)
        with patch.object(self.store, 'create_object', side_effect=fail):
            self.assertEqual(self.worker.run_once()[0]['state'], 'failed')
        self.assertEqual(self.store.list_objects(self.pid, kind='media'), before)
        self.assertNotIn('prepared_frames', self.store.get_object(self.pid, job['object_id'])['body'])
        self.assertEqual(self.r.calls, [])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)

    def test_frame_preparation_restart_reuses_exact_bytes_without_extraction(self):
        job = self.enqueue()
        self.jobs.clock = lambda: 1000
        with patch.object(self.jobs, 'begin_dispatch', side_effect=DomainError('revision_conflict', 'before paid')):
            self.assertEqual(self.worker.run_once()[0]['state'], 'queued')
        prepared = self.store.get_object(self.pid, job['object_id'])['body']['prepared_frames']
        restarted = self.restart()
        restarted.jobs.clock = lambda: 1301
        with patch('production.worker.FrameEvidence.extract', side_effect=AssertionError('Must reuse exact prepared frames')):
            self.assertEqual(restarted.run_once()[0]['state'], 'succeeded')
        self.assertEqual(self.store.get_object(self.pid, job['object_id'])['body']['prepared_frames'], prepared)
        self.assertEqual(len(self.r.calls), 1)

    def test_expired_frame_preparation_cannot_publish_or_refund_new_owner(self):
        from production.frame_evidence import FrameEvidence
        job = self.enqueue()
        self.jobs.clock = lambda: 1000
        other = self.f.auth.authenticate(self.f.auth.provision_token('frame_other', 'worker', [self.pid], 300))
        extract = FrameEvidence.extract
        def expire(extractor, *args):
            frames = extract(extractor, *args)
            self.jobs.clock = lambda: 1301
            self.jobs.claim(other, self.pid, job['object_id'])
            return frames
        with patch.object(FrameEvidence, 'extract', expire):
            self.assertEqual(self.worker.run_once()[0]['state'], 'queued')
        current = self.store.get_object(self.pid, job['object_id'])
        self.assertNotIn('prepared_frames', current['body'])
        self.assertEqual(current['body']['lease']['credential_id'], other.credential_id)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='media')), 1)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 10)
        self.assertEqual(self.r.calls, [])

    def test_corrupted_prepared_frame_fails_before_paid_restart(self):
        job = self.enqueue()
        self.jobs.clock = lambda: 1000
        with patch.object(self.jobs, 'begin_dispatch', side_effect=DomainError('revision_conflict', 'before paid')):
            self.assertEqual(self.worker.run_once()[0]['state'], 'queued')
        pack = self.store.get_object(self.pid, job['object_id'])['body']['prepared_frames']
        frame = pack['frames'][0]['media']
        path = self.f.media.path_for(self.pid, frame['object_id'])
        path.chmod(0o600)
        path.write_bytes(b'corrupted')
        restarted = self.restart()
        restarted.jobs.clock = lambda: 1301
        self.assertEqual(restarted.run_once()[0]['state'], 'failed')
        self.assertEqual(self.r.calls, [])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)

    def test_short_lease_blocks_before_claim_or_any_paid_call(self):
        self.jobs.lease_seconds = 65  # Published observation timeout is 120 seconds.
        self.enqueue()
        self.assertEqual(self.worker.run_once()[0]['state'], 'queued')
        self.assertEqual(self.r.calls, [])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 10)
        self.assertEqual(self.store.list_objects(self.pid, kind='provider-attempt'), [])

    def test_missing_legacy_frame_policy_does_not_invent_frame_evidence(self):
        self.r.profile.pop('frame_evidence')
        self.r.publish()
        self.worker.reader = Reader(self.jobs, self.f.flow.config, transport=self.r.transport)
        job = self.enqueue()
        self.assertEqual(self.worker.run_once()[0]['state'], 'succeeded')
        current = self.store.get_object(self.pid, job['object_id'])
        observation = self.store.get_object(self.pid, current['body']['result']['object_id'])
        self.assertEqual(observation['body'].get('frames', []), [])
        self.assertNotIn('prepared_frames', current['body'])

    def test_observer_only_bootstrap_has_no_hf_or_fake_endpoint_option(self):
        token = self.f.auth.provision_token('bootstrap_observer','worker',[self.pid],300)
        config = {'storage':{'mode':'development','database':str(self.store.path.resolve()),'media_root':str(self.f.media.root.resolve())},
            'public_origin':'https://studio.example',
            'project_ids':[self.pid],'worker_token_env':'MVGP_TEST_OBSERVER_TOKEN','reader_enabled':True,
            'ffmpeg_path':str(Path(shutil.which('ffmpeg')).resolve())}
        path = self.f.root/'observer-private.json'
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        with patch.dict(os.environ,{'MVGP_TEST_OBSERVER_TOKEN':token}):
            service = build_worker(path)
        self.assertEqual(service.operations,['observe'])
        self.assertIsNone(service.provider)
        self.assertFalse(service.reader.fake)
        config['reader_endpoint'] = 'https://untrusted.example'
        path.write_text(json.dumps(config))
        with self.assertRaises(DomainError):
            build_worker(path)


class CutWorkerTests(unittest.TestCase):
    def setUp(self):
        self.c = test_cuts.CutTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.f,self.store,self.pid = self.c.f,self.c.store,self.c.pid
        self.cut = self.c.service.create(self.c.actor,self.pid,self.c.request())
        self.job = self.c.submissions.render_cut(self.c.actor,self.pid,RenderCutRequest(idempotency_key='worker-cut',
            expected_revision=self.cut['revision'],cut=self.f.ref(self.cut)))

    def restart(self, now=None):
        store = Store(self.store.path)
        auth = AuthService(store,'https://studio.example')
        actor = auth.authenticate(auth.provision_token('cut_worker','worker',[self.pid],300))
        media = MediaStore(store,self.f.media.root)
        flow = Workflow(store,auth,self.f.flow.config)
        jobs = Jobs(store,auth,Submissions(store,auth,flow,Gates(store,flow,media)),media,lease_seconds=300)
        if now is not None:
            jobs.clock = lambda:now
        return Worker(jobs,None,actor,[self.pid],cuts=Cuts(store,auth,flow,media))

    def test_restart_local_dispatch_retries_exact_intent_and_real_render(self):
        self.c.jobs.clock = lambda:1000
        claim = self.c.jobs.claim(self.c.worker,self.pid,self.job['object_id'])
        old = self.c.jobs.begin_dispatch(self.c.worker,self.pid,self.f.ref(claim['job']),claim['fence'])
        worker = self.restart(1301)
        self.assertEqual(worker.run_once()[0]['state'],'succeeded')
        latest = self.store.get_object(self.pid,self.job['object_id'])
        media = self.store.get_object(self.pid,latest['body']['result']['object_id'])
        self.assertEqual(media['body']['source_cut'],self.f.ref(self.cut).model_dump())
        self.assertFalse(media['body']['accepted'])
        self.assertAlmostEqual(media['body']['probe']['duration'],1.0,delta=0.09)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),2)
        with self.assertRaises(DomainError):
            self.c.service.render(self.c.worker,self.pid,self.f.ref(self.cut),jobs=self.c.jobs,
                                  job_ref=self.f.ref(old['job']),fence=claim['fence'])

    def test_local_failure_retry_bound_and_no_paid_budget(self):
        worker = self.restart()
        with patch.object(worker.cuts,'render',side_effect=DomainError('provider_failure','Local fixture failure')):
            for _ in range(4):
                result = worker.run_once()[0]
        self.assertEqual(result['state'],'failed')
        self.assertEqual(worker.run_once(),[])
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),4)
        self.assertEqual(self.store.list_objects(self.pid,kind='observation'),[])


class AutoFilmReleaseTests(unittest.TestCase):
    """Rehearsal finding: a cut made under an earlier release is recut, never reused as the film."""

    def setUp(self):
        from production.tests import test_projects
        from production.worker import AutoFilm
        self.p = test_projects.ProjectTests()
        self.p.setUp()
        self.addCleanup(self.p.tearDown)
        self.store, self.pid = self.p.store, self.p.pid
        project = self.store.get_object(self.pid, self.pid)
        self.store.append_revision(self.pid, self.pid, project['revision'], {**project['body'], 'release_id': 'release_' + 'b' * 64}, 'operator')
        ref = self.p.ref
        receipt = self.store.create_object(self.pid, 'human-receipt', {'verified_human_session': True}, 'decision_service')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S01-010A'}}, 'author_1')
        self.take = self.store.create_object(self.pid, 'media', {'media_type': 'video/mp4', 'probe': {'duration': 8.0, 'has_video': True}},
                                             'worker_service')
        self.store.create_object(self.pid, 'decision-request', {'target': ref(shot), 'purpose': 'take', 'state': 'confirmed',
            'evidence': {'shot': ref(shot), 'takes': [ref(self.take)]}}, 'decision_service')
        self.store.create_object(self.pid, 'human-take-selection', {'shot': ref(shot), 'take': ref(self.take),
            'human_receipt': ref(receipt), 'verified_human_session': True}, 'decision_service')
        self.cuts, self.submissions, decisions = MagicMock(), MagicMock(), MagicMock()
        for service in (self.cuts, self.submissions, decisions):
            service.store = self.store
        principal = MagicMock(role='agent')
        self.film = AutoFilm(principal, self.cuts, self.submissions, decisions)

    def cut(self, release):
        segment = {'take': self.p.ref(self.take), 'start_seconds': 0.0, 'end_seconds': 8.0}
        self.store.create_object(self.pid, 'cut', {'release_id': release, 'segments': [segment]}, 'cut_service')

    def test_cut_from_an_earlier_release_is_recut_under_the_current_one(self):
        self.cut('release_' + 'a' * 64)
        self.assertEqual(self.film.advance(self.pid), 'cut-created')
        self.cuts.create.assert_called_once()
        self.submissions.render_cut.assert_not_called()

    def test_cut_from_the_current_release_goes_on_to_render(self):
        self.cut('release_' + 'b' * 64)
        self.assertEqual(self.film.advance(self.pid), 'render-requested')
        self.cuts.create.assert_not_called()
        self.submissions.render_cut.assert_called_once()


class FalWorkerTests(unittest.TestCase):
    """fal drafts and their 1080p completion run on the worker's paid path, routed by job type."""
    @classmethod
    def setUpClass(cls):
        from production.tests.test_submissions import FalCompletionTests
        FalCompletionTests.setUpClass()
        cls.videos = (FalCompletionTests.draft_video, FalCompletionTests.complete_video)

    def setUp(self):
        from production.provider_fal import ENDPOINTS, FalSeedance
        from production.provider_router import ProviderRouter
        from production.tests.test_provider_fal import CAPABILITY
        from production.tests.test_submissions import FalCompletionTests
        self.fal = FalCompletionTests()
        self.fal.draft_video, self.fal.complete_video = self.videos
        self.fal.start = getattr(self, 'start', 1000.0)
        self.fal.setUp()
        self.addCleanup(self.fal.doCleanups)
        self.sent, self.status = [], 'IN_PROGRESS'
        rid = '01a0d7fd-15d3-7b00-923a-3cc37b30b3b7'
        def transport(method, url, headers, json_body, data, timeout):
            if method == 'POST':
                self.sent.append((url, json_body))
                return 200, json.dumps({'status': 'IN_QUEUE', 'request_id': rid}).encode()
            if url.endswith('/status'):
                return 200, json.dumps({'status': self.status}).encode()
            return 200, json.dumps({'video': {'url': 'https://v3b.fal.media/files/c.mp4', 'content_type': 'video/mp4'},
                                    'seed': 7, 'draft_id': None}).encode()
        adapter = FalSeedance(CAPABILITY, transport=transport, timeout=5, clock=lambda: self.fal.now)
        self.endpoints = ENDPOINTS
        self.router = ProviderRouter({'fal_seedance_2_5': adapter, 'fal_seedance_2_5_complete': adapter})
        self.worker = Worker(self.fal.jobs, self.router, self.fal.worker, [self.fal.pid])

    def test_the_picked_draft_is_sent_polled_and_brought_back_at_1080p(self):
        fal = self.fal
        fal.pick(fal.takes[0])
        job = fal.complete(fal.takes[0])
        first = self.worker.run_once()
        self.assertEqual(first[0]['state'], 'submitted')
        (url, body), = self.sent
        self.assertTrue(url.endswith(self.endpoints['fal_seedance_2_5_complete']))
        self.assertEqual(body, {'draft_id': 'draft_0', 'resolution': '1080p'})
        fal.now += 10
        self.assertEqual(self.worker.run_once()[0]['state'], 'running')
        self.status, fal.video = 'COMPLETED', fal.complete_video
        fal.now += 10
        done = self.worker.run_once()
        self.assertEqual(done[0]['state'], 'succeeded')
        media = fal.store.get_object(fal.pid, fal.store.get_object(fal.pid, job['object_id'])['body']['result']['object_id'])
        self.assertEqual(media['body']['completes'], fal.f.ref(fal.takes[0]).model_dump())
        self.assertEqual(media['body']['provenance']['output_conformance']['observed']['display_height'], 1080)
        self.assertEqual(len(self.sent), 1)  # sent once, never again


class AutoCompleteTests(unittest.TestCase):
    """Ten minutes after the owner's pick, its fal draft is completed once."""
    @classmethod
    def setUpClass(cls):
        FalWorkerTests.setUpClass.__func__(cls)

    def setUp(self):
        from production.worker import AutoComplete
        self.start = time.time()  # pick times are the store's real record times
        FalWorkerTests.setUp(self)
        self.offset = 0.0
        self.auto = AutoComplete(self.fal.worker, self.fal.s.service, clock=lambda: time.time() + self.offset)

    def intents(self):
        return [i for i in self.fal.store.list_objects(self.fal.pid, kind='dispatch-intent')
                if i['body'].get('operation') == 'complete-draft']

    def test_a_pick_is_completed_once_after_ten_minutes(self):
        fal = self.fal
        fal.pick(fal.takes[0])
        self.assertEqual(self.auto.advance(fal.pid), 'idle')  # the owner may still change his mind
        self.offset = 601
        fal.s.service.clock = lambda: time.time() + self.offset
        self.assertEqual(self.auto.advance(fal.pid), 'completing:1')
        self.assertEqual(self.auto.advance(fal.pid), 'idle')
        self.assertEqual(len(self.intents()), 1)

    def test_a_pick_changed_inside_ten_minutes_is_never_completed_and_expired_drafts_are_skipped(self):
        fal = self.fal
        fal.pick(fal.takes[0])
        fal.pick(fal.takes[1])
        self.offset = 601
        fal.s.service.clock = lambda: time.time() + self.offset
        self.auto.advance(fal.pid)
        self.assertEqual([i['body']['target']['object_id'] for i in self.intents()], [fal.takes[1]['object_id']])
        self.offset = 8 * 86400  # past both drafts' seven days
        fal.pick(fal.takes[0])
        self.assertEqual(self.auto.advance(fal.pid), 'idle')

    def test_the_hf_listing_never_settles_a_fal_or_apilio_job(self):
        # A job type with no listing adapter is never closed as absent: the router only lists HF.
        self.assertFalse(self.router.can_list('fal_seedance_2_5'))
        self.assertFalse(self.router.can_list('fal_seedance_2_5_complete'))
        self.assertFalse(self.router.can_list('apilio_gpt_image_2_5'))


    def due(self):
        fal = self.fal
        fal.pick(fal.takes[0])
        self.offset = 601
        fal.s.service.clock = lambda: time.time() + self.offset

    def stops(self):
        return [e['body'] for e in self.fal.store.events(self.fal.pid) if e['kind'] == 'completion.stopped']

    def test_a_budget_refusal_is_recorded_once_where_the_desk_can_show_it(self):
        # never 正片生成中 forever.
        fal = self.fal
        budget = fal.store.budget(fal.pid)
        fal.store.set_budget(fal.pid, budget['spent'] + budget['reserved'] + 1, 'credit')
        self.due()
        self.assertEqual(self.auto.advance(fal.pid), 'blocked:budget_exceeded')
        self.auto.advance(fal.pid)
        self.assertEqual([(b['code'], b['take']['object_id']) for b in self.stops()], [('budget_exceeded', fal.takes[0]['object_id'])])
        fal.store.set_budget(fal.pid, 1000, 'credit')
        self.assertEqual(self.auto.advance(fal.pid), 'completing:1')  # once the envelope is raised it goes ahead

    def test_a_completion_that_cost_nothing_is_retried_and_the_last_refusal_is_recorded(self):
        fal = self.fal
        fal.s.policy['operations']['complete-draft']['fal_seedance_2_5_complete']['max_attempts'] = 2
        fal.s.f.config.set('execution_policy', fal.s.policy)
        fal.store.set_budget(fal.pid, 1000, 'credit')
        self.due()
        self.assertEqual(self.auto.advance(fal.pid), 'completing:1')
        fal.finish_completion(fal.store.get_object(fal.pid, self.jobs_of_completions()[-1]), 'failed', 0)
        self.assertEqual(self.auto.advance(fal.pid), 'completing:1')  # it never ran: try again
        self.assertEqual(len(self.intents()), 2)
        fal.finish_completion(fal.store.get_object(fal.pid, self.jobs_of_completions()[-1]), 'failed', 0)
        self.assertEqual(self.auto.advance(fal.pid), 'blocked:attempt_limit')
        self.assertEqual([b['code'] for b in self.stops()], ['attempt_limit'])

    def jobs_of_completions(self):
        ids = {i['object_id'] for i in self.intents()}
        jobs = [j for j in self.fal.store.list_objects(self.fal.pid, kind='job') if j['body']['intent']['object_id'] in ids]
        return [j['object_id'] for j in sorted(jobs, key=lambda j: j['body']['attempt'])]


class ExpiredCompletionTests(unittest.TestCase):
    """Bug hunt 2026-09-25: a completion queued before its draft expired, dispatched after, is cancelled and released."""
    @classmethod
    def setUpClass(cls):
        FalWorkerTests.setUpClass.__func__(cls)

    def setUp(self):
        FalWorkerTests.setUp(self)

    def test_the_worker_releases_the_hold_of_a_completion_whose_draft_expired_in_the_queue(self):
        fal = self.fal
        fal.pick(fal.takes[0])
        job = fal.complete(fal.takes[0])
        held = fal.store.budget(fal.pid)['reserved']
        fal.now += 7 * 86400  # the worker was down past the draft's seven days
        outcome = self.worker.run_once()[0]
        self.assertEqual(outcome['error_code'], 'stale_input')
        self.assertEqual(self.sent, [])
        self.assertEqual(fal.store.get_object(fal.pid, job['object_id'])['body']['state'], 'cancelled')
        self.assertLess(fal.store.budget(fal.pid)['reserved'], held)
