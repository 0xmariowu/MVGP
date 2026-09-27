"""Operator-only credential and envelope boundaries, with no provider calls."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from production.auth import SYSTEM_PROJECT, AuthService
from production.contracts import DomainError, ObjectRef
from production.jobs import Jobs
from production.media import MediaStore
from production.operations import (
    AbandonObservationRequest,
    BackupRequest,
    EnvelopeRequest,
    IssueRequest,
    Operations,
    OperatorConfig,
    RestoreRequest,
    RetryResultDownloadRequest,
    RotateRequest,
    load_config,
    main,
)
from production.store import Store
from production.tests.fixtures import Authority, Transport


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.secrets = self.root/'credentials'
        self.secrets.mkdir(mode=0o700)
        self.config = OperatorConfig(database=str(self.root/'store.sqlite'), public_origin='https://film.example',
            operator_id='local_operator', credential_directory=str(self.secrets))
        self.operations = Operations(self.config)
        self.store, self.auth = self.operations.store, self.operations.auth
        self.store.create_project('one', {}, 'fixture')
        self.store.create_project('two', {}, 'fixture')
        self.config_path = self.root/'operator.json'
        self.config_path.write_text(self.config.model_dump_json())
        self.config_path.chmod(0o600)

    def issue(self, role='agent', **kwargs):
        return self.operations.issue(IssueRequest(actor_id='maker', role=role, project_ids=['one'],
            ttl_seconds=300, output_name=role+'.token', **kwargs))

    def token(self, result):
        return Path(result['token_file']).read_text().strip()

    def result_download(self, *, changes=None, author='worker_service', kind='job',
                        receipt_author='worker_service', receipt_kind='provider-receipt', receipt_changes=None):
        ref = lambda obj: {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        private = self.store.create_object(SYSTEM_PROJECT, 'provider-download',
            {'project_id': 'one', 'url': 'https://cdn.example/result.mp4'}, 'worker_service')
        receipt = self.store.create_object('one', receipt_kind,
            {'download_reference': ref(private), **(receipt_changes or {})}, receipt_author)
        job = self.store.create_object('one', kind, {'state': 'unknown', 'lease': None,
            'pending_result': ref(receipt), 'download_count': 3, 'poll_count': 2, 'remote_job_id': 'remote1',
            'last_error': 'invalid_media', **(changes or {})}, author)
        self.store.set_budget('one', 1000, 'credit')
        self.store.reserve('one', job['object_id'], 10, 'credit', object_id=job['object_id'])
        self.store.settle('one', job['object_id'], None)
        return RetryResultDownloadRequest(project_id='one', job=ObjectRef(**ref(job)), reason='Fixed host ffprobe PATH')

    def download_retry_snapshot(self):
        with self.store.transaction(write=False) as db:
            return {table: [dict(row) for row in db.execute(f'SELECT * FROM {table}')]
                    for table in ('objects', 'revisions', 'events', 'reservations', 'budgets')}

    def assert_download_retry_refused(self, request, code):
        before = self.download_retry_snapshot()
        with self.assertRaises(DomainError) as caught:
            self.operations.retry_result_download(request)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.download_retry_snapshot(), before)

    def test_retry_result_download_resets_only_allowance_and_audits_then_claims(self):
        for error in ('invalid_media', 'provider_failure'):
            for author in ('worker_service', 'submission_service'):
                with self.subTest(error=error, author=author):
                    request = self.result_download(changes={'last_error': error}, author=author)
                    previous = self.store.get_object('one', request.job.object_id)
                    before = self.download_retry_snapshot()
                    result = self.operations.retry_result_download(request)
                    updated = self.store.get_object('one', request.job.object_id)
                    self.assertEqual(updated['revision'], previous['revision'] + 1)
                    self.assertEqual(updated['author'], author)
                    self.assertEqual(updated['body'], {**previous['body'], 'download_count': 0, 'last_error': None})
                    after = self.download_retry_snapshot()
                    for table in ('reservations', 'budgets'):
                        self.assertEqual(after[table], before[table])
                    self.assertEqual(len(after['objects']), len(before['objects']))
                    self.assertEqual(len(after['revisions']), len(before['revisions']) + 1)
                    audit = [event for event in self.store.events('one')
                             if event['kind'] == 'operator.result_download.retried' and event['body']['job'] == result['job']]
                    self.assertEqual(len(audit), 1)
                    self.assertEqual(audit[0]['body'], {'operator_id': self.config.operator_id, 'job': result['job'],
                        'previous_download_count': 3, 'previous_last_error': error, 'reason': request.reason})
                    self.assertEqual(result, {'job': {key: updated[key] for key in ('object_id', 'revision', 'digest')},
                        'state': 'unknown', 'billing_changed': False})
                    self.assert_download_retry_refused(request, 'revision_conflict')
                    worker = self.auth.authenticate(self.auth.provision_token('worker', 'worker', ['one'], 300))
                    # Claim uses only the store and auth; no provider or media operation is needed.
                    jobs = Jobs(self.store, self.auth, None, None)
                    claim = jobs.claim(worker, 'one', request.job.object_id)
                    self.assertEqual(claim['action'], 'download')
                    self.assertEqual(claim['job']['body']['pending_result'], previous['body']['pending_result'])

    def test_retry_result_download_requires_current_revision_and_digest(self):
        request = self.result_download()
        for change, code in (({'digest': '0' * 64}, 'revision_conflict'), ({'digest': None}, 'invalid_input')):
            with self.subTest(change=change):
                self.assert_download_retry_refused(request.model_copy(update={
                    'job': request.job.model_copy(update=change)}), code)
        job = self.store.get_object('one', request.job.object_id)
        self.store.append_revision('one', job['object_id'], job['revision'], job['body'], job['author'])
        self.assert_download_retry_refused(request, 'revision_conflict')

    def test_retry_result_download_rejects_ineligible_jobs_and_receipts(self):
        cases = [
            {'kind': 'brief'}, {'author': 'agent'},
            *({'changes': {'state': state}} for state in ('queued', 'dispatching', 'running', 'submitted', 'succeeded', 'failed', 'cancelled')),
            {'changes': {'lease': {}}}, {'changes': {'lease': {'expires_at': 0}}},
            {'changes': {'pending_result': None}}, {'changes': {'pending_result': {'object_id': 'bad'}}},
            {'receipt_author': 'agent'}, {'receipt_kind': 'brief'}, {'receipt_changes': {'download_reference': None}},
            *({'changes': {'last_error': error}} for error in (None, 'unknown_outcome', 'forbidden', 'unsupported_route')),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assert_download_retry_refused(self.result_download(**case), 'forbidden')

    def test_retry_result_download_resolves_exact_receipt_reference(self):
        request = self.result_download()
        job = self.store.get_object('one', request.job.object_id)
        reference = job['body']['pending_result']
        for change, code in (({'digest': '0' * 64}, 'forbidden'), ({'digest': None}, 'forbidden'),
                             ({'revision': reference['revision'] + 1}, 'not_found')):
            with self.subTest(change=change):
                job = self.store.append_revision('one', job['object_id'], job['revision'],
                    {**job['body'], 'pending_result': {**reference, **change}}, job['author'])
                bad = request.model_copy(update={'job': ObjectRef(**{key: job[key] for key in ('object_id', 'revision', 'digest')})})
                self.assert_download_retry_refused(bad, code)
        # A later receipt revision cannot replace the exact historical receipt.
        self.store.append_revision('one', reference['object_id'], reference['revision'], {}, 'agent')
        job = self.store.append_revision('one', job['object_id'], job['revision'],
            {**job['body'], 'pending_result': reference}, job['author'])
        request = request.model_copy(update={'job': ObjectRef(**{key: job[key] for key in ('object_id', 'revision', 'digest')})})
        self.assertEqual(self.operations.retry_result_download(request)['state'], 'unknown')

    def test_retry_result_download_audit_failure_rolls_back(self):
        request = self.result_download()
        before = self.download_retry_snapshot()
        with patch.object(self.operations, '_audit', side_effect=RuntimeError('audit failed')), self.assertRaises(RuntimeError):
            self.operations.retry_result_download(request)
        self.assertEqual(self.download_retry_snapshot(), before)
        self.assertEqual(self.operations.retry_result_download(request)['state'], 'unknown')

    def test_retry_result_download_cli_reads_json(self):
        request = self.result_download()
        path = self.root / 'retry.json'
        path.write_text(request.model_dump_json())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--config', str(self.config_path), 'retry-result-download', '--input', str(path)]), 0)
        result = json.loads(out.getvalue())
        updated = self.store.get_object('one', request.job.object_id)
        self.assertEqual(result['job']['revision'], request.job.revision + 1)
        self.assertEqual(updated['body']['download_count'], 0)
        self.assertIsNone(updated['body']['last_error'])

    def test_effective_grants_and_token_only_private_file(self):
        for role, allowed in [('agent', 'prepare'), ('viewer', 'media'), ('worker', 'dispatch')]:
            result = self.issue(role)
            token = self.token(result)
            self.assertNotIn(token, json.dumps(result))
            self.assertEqual(Path(result['token_file']).stat().st_mode & 0o777, 0o600)
            actor = self.auth.authenticate(token)
            self.auth.authorize(actor, 'one', allowed)
            with self.assertRaises(DomainError):
                self.auth.authorize(actor, 'two', allowed)
            with self.assertRaises(DomainError):
                self.auth.authorize(actor, 'one', 'human-decision')
            with self.assertRaises(DomainError):
                self.auth.authorize(actor, 'one', 'record-review', target='arbitrary')
            credential = self.store.get_object(SYSTEM_PROJECT, result['credential_id'])
            self.assertNotIn(token, json.dumps(credential))
        for role in ('reviewer', 'human-session', 'operator'):
            with self.assertRaises(ValidationError):
                IssueRequest(actor_id='maker', role=role, project_ids=['one'], output_name='bad.token')

    def test_existing_destination_refuses_without_issuing(self):
        path = self.secrets/'agent.token'
        path.write_text('retained')
        before = self.store.list_objects(SYSTEM_PROJECT)
        with self.assertRaises(DomainError):
            self.issue()
        self.assertEqual(path.read_text(), 'retained')
        self.assertEqual(self.store.list_objects(SYSTEM_PROJECT), before)

    def test_file_failure_rolls_back_credential_and_cleans_only_own_file(self):
        before = self.store.list_objects(SYSTEM_PROJECT)
        with patch('production.operations.os.fsync', side_effect=OSError('disk unavailable')), self.assertRaises(DomainError):
            self.issue()
        self.assertFalse((self.secrets/'agent.token').exists())
        self.assertEqual(self.store.list_objects(SYSTEM_PROJECT), before)

    def test_rotate_preserves_scope_and_revoke_is_revision_guarded(self):
        old = self.issue()
        fresh = self.operations.rotate(RotateRequest(credential_id=old['credential_id'], expected_revision=old['revision'],
            ttl_seconds=200, output_name='rotated.token'))
        with self.assertRaises(DomainError):
            self.auth.authenticate(self.token(old))
        actor = self.auth.authenticate(self.token(fresh))
        self.assertEqual(actor.role, 'agent')
        self.assertEqual(actor.project_ids, frozenset({'one'}))
        self.operations.revoke(fresh['credential_id'], fresh['revision'])
        with self.assertRaises(DomainError):
            self.auth.authorize(actor, 'one', 'prepare')
        with self.assertRaises(DomainError):
            self.operations.revoke(fresh['credential_id'], fresh['revision'])

    def test_all_projects_worker_is_issued_audited_and_kept_by_rotation(self):

        worker = self.operations.issue(IssueRequest(actor_id='worker_all', role='worker', project_ids=[],
            ttl_seconds=300, output_name='worker-all.token', all_projects=True))
        principal = self.auth.authenticate(self.token(worker))
        self.assertIn('one', self.auth.dispatch_scope(principal))
        rotated = self.operations.rotate(RotateRequest(credential_id=worker['credential_id'], expected_revision=worker['revision'],
            ttl_seconds=200, output_name='worker-all-2.token'))
        self.assertIn('one', self.auth.dispatch_scope(self.auth.authenticate(self.token(rotated))))
        for bad in ({'role': 'agent', 'allow_create_project': True}, {'role': 'viewer'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                IssueRequest(actor_id='x', project_ids=[], output_name='x.token', all_projects=True, **bad)

    def test_failed_rotation_keeps_old_credential_valid(self):
        old = self.issue()
        with self.assertRaises(DomainError):
            self.operations.rotate(RotateRequest(credential_id=old['credential_id'], expected_revision=old['revision'],
                ttl_seconds=200, output_name='agent.token'))
        self.auth.authenticate(self.token(old))

    def test_config_paths_ownership_permissions_and_unknown_fields(self):
        self.assertEqual(load_config(self.config_path), self.config)
        self.config_path.chmod(0o644)
        with self.assertRaises(DomainError):
            load_config(self.config_path)
        self.config_path.chmod(0o600)
        link = self.root/'link'
        link.symlink_to(self.config_path)
        with self.assertRaises(DomainError):
            load_config(link)
        with self.assertRaises(DomainError):
            load_config(Path('relative.json'))
        self.config_path.write_text(self.config.model_dump_json()[:-1]+',"api_key":"private"}')
        with self.assertRaises(DomainError):
            load_config(self.config_path)
        self.secrets.chmod(0o755)
        with self.assertRaises(DomainError):
            Operations(self.config)

    def test_envelope_guards_and_commitments_with_audit(self):
        first = self.operations.set_envelope(EnvelopeRequest(project_id='one', expected_revision=0,
            expected_ceiling=None, limit=10, unit='credits', reason='Initial test envelope'))
        self.store.reserve('one', 'reservation', 7, 'credits')
        self.store.settle('one', 'reservation', None)
        with self.assertRaises(DomainError):
            self.operations.set_envelope(EnvelopeRequest(project_id='one', expected_revision=first['revision'],
                expected_ceiling=10, limit=6, unit='credits', reason='Invalid decrease'))
        self.store.set_budget('one', 12, 'credits')
        with self.assertRaises(DomainError):
            self.operations.set_envelope(EnvelopeRequest(project_id='one', expected_revision=first['revision'],
                expected_ceiling=10, limit=20, unit='credits', reason='Stale operator view'))
        current = self.operations.envelope('one')
        self.assertEqual(current['reserved'], 7)
        next_budget = self.operations.set_envelope(EnvelopeRequest(project_id='one', expected_revision=current['revision'],
            expected_ceiling=12, limit=15, unit='credits', reason='Explicit operator increase'))
        self.assertGreater(next_budget['revision'], current['revision'])
        audit = [event for event in self.store.events('one') if event['kind']=='operator.envelope.changed']
        self.assertEqual(audit[-1]['body']['operator_id'], 'local_operator')

    def test_concurrent_envelope_updates_have_one_winner(self):
        start = self.operations.set_envelope(EnvelopeRequest(project_id='one', expected_revision=0,
            expected_ceiling=None, limit=10, unit='credits', reason='Initial envelope'))
        outcomes, barrier = [], threading.Barrier(2)
        def run(limit):
            barrier.wait()
            try:
                self.operations.set_envelope(EnvelopeRequest(project_id='one', expected_revision=start['revision'],
                    expected_ceiling=10, limit=limit, unit='credits', reason='Concurrent operation'))
                outcomes.append('ok')
            except DomainError as exc:
                outcomes.append(exc.code)
        threads=[threading.Thread(target=run,args=(value,)) for value in (11,12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertCountEqual(outcomes,['ok','revision_conflict'])

    def test_named_envelopes_have_independent_native_units_and_revision_guards(self):
        def request(key, limit, unit, previous=None):
            return EnvelopeRequest(project_id='one', budget_key=key,
                expected_revision=previous['revision'] if previous else 0,
                expected_ceiling=previous['ceiling'] if previous else None,
                limit=limit, unit=unit, reason='Separate provider-native account allowance')
        hf = self.operations.set_envelope(request('hf_primary', 10, 'credit'))
        usd = self.operations.set_envelope(request('deepseek_primary', 100, 'usd_micro'))
        self.assertEqual(self.operations.envelope('one', budget_key='hf_primary'), hf)
        self.operations.set_envelope(request('hf_primary', 20, 'credit', hf))
        self.operations.set_envelope(request('deepseek_primary', 200, 'usd_micro', usd))
        with self.assertRaises(DomainError):
            self.operations.set_envelope(request('hf_primary', 30, 'credit', hf))
        self.assertEqual(self.store.budget('one', budget_key='deepseek_primary')['ceiling'], 200)
        with self.assertRaises(DomainError):
            self.store.budget('one')

    def billing_fixture(self, *, pid='one', suffix='a', amount=3, outcome='billed', ledger='entry-1', provider='deepseek'):
        from production.operations import ReconcileCostRequest
        self.store.set_budget(pid, 10, 'usd_micro', budget_key='deepseek_primary')
        owner = self.store.create_object(pid, 'review-task', {'state':'completed'}, 'review_service')
        reservation = 'reservation_'+suffix
        self.store.reserve(pid, reservation, 7, 'usd_micro', budget_key='deepseek_primary', object_id=owner['object_id'])
        self.store.settle(pid, reservation, None)
        target = {k:owner[k] for k in ('object_id','revision','digest')}
        proof = {'schema_version':1, 'project_id':pid, 'reservation_id':reservation,
            'target':target, 'budget_key':'deepseek_primary', 'unit':'usd_micro',
            'provider':provider, 'ledger_entry_id':ledger, 'provider_request_id':'req-'+suffix,
            'actual_amount':amount, 'outcome':outcome,
            'verification':'Operator matched this exact request to the private supplier transaction export.'}
        path = self.root/('billing-'+suffix+'.json')
        path.write_text(json.dumps(proof)); path.chmod(0o600)
        request = ReconcileCostRequest(idempotency_key='cost-'+suffix, project_id=pid,
            reservation_id=reservation, target=target, expected_reserved_amount=7,
            evidence_file=str(path), evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            reason='Reconcile a known supplier ledger entry; not an estimate.')
        return request, proof, owner

    def test_unknown_cost_reconciles_once_without_task_or_approval_mutation(self):
        request, proof, owner = self.billing_fixture()
        result = self.operations.reconcile_cost(request)
        self.assertEqual(result['budget']['spent'], 3)
        self.assertEqual(result['budget']['reserved'], 0)
        self.assertEqual(self.operations.reconcile_cost(request), result)
        self.assertEqual(self.store.get_object('one',owner['object_id']),owner)
        serialized = json.dumps(result)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(proof['provider_request_id'], serialized)
        self.assertFalse(result['task_outcome_changed'])
        events = [e for e in self.store.events('one') if e['kind']=='operator.cost.reconciled']
        self.assertEqual(len(events),1)

    def test_a_fal_charge_settles_from_the_fal_usage_entry(self):
        # fal's own usage export is the bill for a fal hold gone unknown.
        request, _, _ = self.billing_fixture(provider='fal', suffix='fal')
        result = self.operations.reconcile_cost(request)
        self.assertEqual((result['budget']['spent'], result['budget']['reserved']), (3, 0))

    def test_cost_reconciliation_rejects_stale_wrong_binding_and_held_reservation(self):
        request, proof, owner = self.billing_fixture()
        for change in ({'project_id':'two'}, {'expected_reserved_amount':6},
                       {'target':ObjectRef(object_id=owner['object_id'],revision=1,digest='0'*64)}):
            with self.subTest(change=change), self.assertRaises(DomainError):
                self.operations.reconcile_cost(request.model_copy(update=change))
        path=Path(request.evidence_file)
        for key,value in [('unit','credit'),('budget_key','other'),('actual_amount',True)]:
            data={**proof,key:value}; path.write_text(json.dumps(data))
            bad=request.model_copy(update={'evidence_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            with self.subTest(key=key), self.assertRaises(DomainError): self.operations.reconcile_cost(bad)
        path.write_text(json.dumps(proof))
        with self.store.transaction() as db:
            db.execute("UPDATE reservations SET state='held' WHERE reservation_id=?",(request.reservation_id,))
        with self.assertRaises(DomainError): self.operations.reconcile_cost(request)
        self.assertEqual(self.store.budget('one',budget_key='deepseek_primary')['spent'],0)

    def test_cost_receipt_is_private_hash_checked_and_unique_across_projects(self):
        request, _, _ = self.billing_fixture()
        with self.assertRaises(DomainError):
            self.operations.reconcile_cost(request.model_copy(update={'evidence_sha256':'0'*64}))
        path=Path(request.evidence_file); path.chmod(0o644)
        with self.assertRaises(DomainError): self.operations.reconcile_cost(request)
        path.chmod(0o600)
        self.operations.reconcile_cost(request)
        duplicate, _, _ = self.billing_fixture(pid='two',suffix='b')
        with self.assertRaises(DomainError): self.operations.reconcile_cost(duplicate)
        self.assertEqual(self.store.budget('two',budget_key='deepseek_primary')['reserved'],7)

    def test_reconciliation_records_overrun_and_requires_explicit_no_charge(self):
        request,_,_=self.billing_fixture(amount=12)
        self.assertEqual(self.operations.reconcile_cost(request)['budget']['spent'],12)
        with self.assertRaises(DomainError):
            self.store.reserve('one','next',1,'usd_micro',budget_key='deepseek_primary')
        zero,proof,_=self.billing_fixture(pid='two',suffix='zero',amount=0,ledger='zero-entry')
        with self.assertRaises(DomainError): self.operations.reconcile_cost(zero)
        path=Path(zero.evidence_file); proof['outcome']='confirmed_no_charge'; path.write_text(json.dumps(proof))
        zero=zero.model_copy(update={'evidence_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.assertEqual(self.operations.reconcile_cost(zero)['budget']['reserved'],0)

    def test_reconciliation_cannot_resolve_active_work_or_change_creative_authority(self):
        request,proof,owner=self.billing_fixture()
        latest=self.store.append_revision('one',owner['object_id'],1,{'state':'running'},'review_service')
        with self.assertRaises(DomainError): self.operations.reconcile_cost(request)
        target={k:latest[k] for k in ('object_id','revision','digest')}
        proof['target']=target
        path=Path(request.evidence_file); path.write_text(json.dumps(proof))
        request=request.model_copy(update={'target':ObjectRef(**target),'evidence_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        with self.assertRaises(DomainError) as error: self.operations.reconcile_cost(request)
        self.assertEqual(error.exception.code,'locked')
        self.assertEqual(self.store.budget('one',budget_key='deepseek_primary')['reserved'],7)

    def test_cost_reconciliation_cli_keeps_receipt_private(self):
        request,proof,_=self.billing_fixture()
        request_path=self.root/'reconcile-request.json'; request_path.write_text(request.model_dump_json())
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['--config',str(self.config_path),'reconcile-cost','--input',str(request_path)])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())['budget']['spent'],3)
        self.assertNotIn(proof['provider_request_id'],output.getvalue())

    def test_creation_capability_is_explicit_and_human_cannot_inherit_it(self):
        issued=self.operations.issue(IssueRequest(actor_id='founder',role='agent',project_ids=[],
            allow_create_project=True,output_name='founder.token'))
        self.auth.authorize(self.auth.authenticate(self.token(issued)),None,'create-project')
        ordinary=self.issue()
        with self.assertRaises(DomainError):
            self.auth.authorize(self.auth.authenticate(self.token(ordinary)),None,'create-project')
        for role in ('worker','viewer','human-exchange'):
            with self.assertRaises(ValidationError):
                IssueRequest(actor_id='bad',role=role,project_ids=['one'],allow_create_project=True,output_name='bad.token')
        for name in ('../outside.token','/absolute.token','nested/file.token'):
            with self.assertRaises(ValidationError):
                IssueRequest(actor_id='bad',role='agent',project_ids=['one'],output_name=name)

    def test_budget_zero_and_invalid_numbers_and_unit_preservation(self):
        base={'project_id':'one','expected_revision':0,'expected_ceiling':None,'limit':0,'unit':'credits','reason':'Zero envelope'}
        current=self.operations.set_envelope(EnvelopeRequest(**base))
        self.assertEqual(current['ceiling'],0)
        for amount in (True,-1,2**63,float('inf'),float('nan'),1.5):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                EnvelopeRequest(**{**base,'limit':amount})
        with self.assertRaises(DomainError):
            self.operations.set_envelope(EnvelopeRequest(**{**base,'expected_revision':current['revision'],
                'expected_ceiling':0,'unit':'different'}))
        current=self.operations.set_envelope(EnvelopeRequest(**{**base,'expected_revision':current['revision'],
                'expected_ceiling':0,'limit':10}))
        self.store.reserve('one','actual-cost',4,'credits')
        self.store.settle('one','actual-cost',4)
        with self.assertRaises(DomainError):
            self.operations.set_envelope(EnvelopeRequest(**{**base,'expected_revision':current['revision'],
                'expected_ceiling':10,'limit':3}))

    def test_cli_secret_redaction_and_exclusive_output(self):
        request=self.root/'request.json'
        request.write_text(IssueRequest(actor_id='creator', role='agent', project_ids=['one'], output_name='cli.token').model_dump_json())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--config',str(self.config_path),'issue','--input',str(request)]),0)
        token=(self.secrets/'cli.token').read_text().strip()
        self.assertNotIn(token,out.getvalue())
        request.write_text('{"api_key":"NEVER_PRINT_THIS"}')
        with contextlib.redirect_stderr(io.StringIO()) as out:
            self.assertEqual(main(['--config',str(self.config_path),'issue','--input',str(request)]),2)
        self.assertNotIn('NEVER_PRINT_THIS',out.getvalue())

    def observation_hold_fixture(self):
        ref = lambda value: {k:value[k] for k in ('object_id', 'revision', 'digest')}
        source = {'object_ref':{'object_id':'source','revision':1,'digest':'a'*64}, 'sha256':'b'*64}
        intent = self.store.create_object('one','dispatch-intent',
            {'operation':'observe','request':{'media':source}},'submission_service')
        job = self.store.create_object('one','job',{'state':'dispatching'},'worker_service')
        attempt = self.store.create_object('one','provider-attempt',
            {'intent':ref(intent),'job_id':job['object_id'],'request':intent['body']['request']},'worker_service')
        observation = self.store.create_object('one','observation',{'status':'unknown','intent':ref(intent),
            'source':source['object_ref'],'source_sha256':source['sha256'],'failure':'transport_outcome_unknown',
            'reservation_action':'hold','authoritative_review':False,'actual_billed_cost':None},'reader_service')
        job = self.store.append_revision('one',job['object_id'],1,{'state':'unknown','lease':None,
            'intent':ref(intent),'attempt_id':attempt['object_id'],'result':ref(observation),
            'observation':ref(observation),'reservation_id':'bill'},'worker_service')
        self.store.set_budget('one',10,'credits',budget_key='observer')
        self.store.reserve('one','bill',7,'credits',budget_key='observer',object_id=intent['object_id'])
        self.store.settle('one','bill',None)
        hold = {'project_id':'one','reservation_id':'bill','budget_key':'observer','unit':'credits',
            'amount':7,'target':ref(intent)}
        acknowledgement = {'project_id':'one','job':ref(job),'observation':ref(observation),
            'attempt':ref(attempt),'reason':'Retain immutable unknown reading; no retry or approval'}
        return hold, acknowledgement, job, observation, attempt, intent

    def test_abandon_observation_preserves_evidence_bill_and_does_not_replay(self):
        _, acknowledgement, job, observation, attempt, intent = self.observation_hold_fixture()
        request = AbandonObservationRequest(**acknowledgement,
            idempotency_key='abandon-observation', acknowledge_unresolved_charge=True)
        budget = self.store.budget('one', budget_key='observer')
        with patch('production.reader.Reader.observe', side_effect=AssertionError('No paid replay')):
            result = self.operations.abandon_observation(request)
            self.assertEqual(self.operations.abandon_observation(request), result)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['provider_outcome'], 'unknown')
        self.assertFalse(result['billing_changed'])
        self.assertFalse(result['approval_issued'])
        self.assertEqual(self.store.budget('one', budget_key='observer'), budget)
        current = self.store.get_object('one', job['object_id'])
        self.assertEqual(current['body']['result'], job['body']['result'])
        self.assertEqual(current['body']['attempt_id'], job['body']['attempt_id'])
        self.assertEqual(self.store.get_object('one', job['object_id'], revision=job['revision']), job)
        for obj in (observation, attempt, intent):
            self.assertEqual(self.store.get_object('one', obj['object_id']), obj)
        self.assertEqual(self.store.list_objects('one', kind='review-receipt'), [])
        with self.store.transaction(write=False) as db:
            self.assertEqual(db.execute("SELECT state FROM reservations WHERE reservation_id='bill'").fetchone()[0], 'unknown')

    def test_abandon_observation_rejects_known_active_generation_stale_and_unacknowledged(self):
        _, acknowledgement, job, observation, attempt, intent = self.observation_hold_fixture()
        request = AbandonObservationRequest(**acknowledgement,
            idempotency_key='abandon-observation', acknowledge_unresolved_charge=True)
        original = self.store.get_object
        cases = [(job, 'state', 'running'), (job, 'lease', {'expires_at': 1}),
            (job, 'remote_job_id', 'remote'), (job, 'pending_result', {'id': 'result'}),
            (observation, 'status', 'succeeded'), (observation, 'raw_response', {'answer': 'yes'}),
            (observation, 'actual_billed_cost', 1), (observation, 'authoritative_review', True),
            (intent, 'operation', 'submit'), (attempt, 'job_id', 'another-job')]
        for obj, key, value in cases:
            def read(pid, oid, obj=obj, key=key, value=value, **kwargs):
                record = original(pid, oid, **kwargs)
                return {**record, 'body': {**record['body'], key: value}} if oid == obj['object_id'] else record
            with self.subTest(key=key), patch.object(self.store, 'get_object', side_effect=read), self.assertRaises(DomainError):
                self.operations.abandon_observation(request)
        with self.assertRaises(DomainError):
            self.operations.abandon_observation(request.model_copy(update={'acknowledge_unresolved_charge': False}))
        with self.assertRaises(DomainError):
            self.operations.abandon_observation(request.model_copy(update={
                'job': request.job.model_copy(update={'digest': '0' * 64})}))
        self.assertEqual(self.store.get_object('one', job['object_id']), job)

    def test_retained_observation_requires_reader_provenance_and_unknown_source_evidence(self):
        from production.operations import RetainedCostHold, RetainedObservation
        hold, acknowledgement, job, observation, *_ = self.observation_hold_fixture()
        for changes,author in (({},'maker'), ({'status':'succeeded'},'reader_service'),
                ({'source_sha256':'0'*64},'reader_service'),
                ({'response_journal':{'pointer':'pending'}},'reader_service'),
                ({'actual_billed_cost':0},'reader_service'),
                ({'authoritative_review':True},'reader_service')):
            current = self.store.get_object('one',observation['object_id'])
            changed = self.store.append_revision('one',observation['object_id'],current['revision'],
                {**observation['body'],**changes},author)
            changed_ref = {k:changed[k] for k in ('object_id','revision','digest')}
            current_job = self.store.get_object('one',job['object_id'])
            changed_job = self.store.append_revision('one',job['object_id'],current_job['revision'],
                {**job['body'],'result':changed_ref,'observation':changed_ref},'worker_service')
            item = {**acknowledgement,'observation':changed_ref,
                'job':{k:changed_job[k] for k in ('object_id','revision','digest')}}
            with self.subTest(changes=changes,author=author), self.store.transaction(write=False) as db, self.assertRaises(DomainError):
                self.operations._retained_observations(db,  # the check abandon-observation runs
                    [RetainedObservation.model_validate(item)],[RetainedCostHold.model_validate(hold)])

    def test_retained_observation_cannot_quarantine_generation(self):
        from production.operations import RetainedCostHold, RetainedObservation
        hold, acknowledgement, job, observation, attempt, intent = self.observation_hold_fixture()
        def revise(record,body):
            return self.store.append_revision('one',record['object_id'],record['revision'],body,record['author'])
        def ref(record): return {k:record[k] for k in ('object_id','revision','digest')}
        intent = revise(intent,{**intent['body'],'operation':'submit'})
        attempt = revise(attempt,{**attempt['body'],'intent':ref(intent)})
        observation = revise(observation,{**observation['body'],'intent':ref(intent)})
        job = revise(job,{**job['body'],'intent':ref(intent),'result':ref(observation),'observation':ref(observation)})
        hold['target'] = ref(intent)
        acknowledgement.update(job=ref(job),observation=ref(observation),attempt=ref(attempt))
        with self.store.transaction(write=False) as db, self.assertRaises(DomainError):
            self.operations._retained_observations(db,[RetainedObservation.model_validate(acknowledgement)],
                [RetainedCostHold.model_validate(hold)])

    def backup_fixture(self):
        media=MediaStore(self.store,self.root/'media')
        item=media.put('one',[b'Archived script bytes'],'text/plain','maker')
        issued=self.issue()
        self.store.create_object(SYSTEM_PROJECT,'review-run-secret',{'token':self.token(issued)},'review_runner_service')
        result=self.operations.backup(BackupRequest(destination=str(self.root/'backup'),media_root=str(media.root)))
        return media,item,issued,result

    def payload_backup_fixture(self):
        from production.review_payloads import ReviewPayloads
        transport = Transport(Authority())
        value = {'model': 'test', 'messages': [{'role': 'user', 'content': 'private-evidence-' + 'a'*1200000}]}
        reference = ReviewPayloads(transport).put(value)
        root = self.store.path.parent/'review-payloads'
        root.mkdir(mode=0o700)
        for digest, data in transport.authority.blobs.items():
            path = root/digest; path.write_bytes(data); path.chmod(0o600)
        turn = self.store.create_object('one', 'review-turn', {'request': reference}, 'review_runner_service')
        return value, reference, turn

    def test_private_backup_restores_external_review_payload_with_original_revision_hash(self):
        from production.review_payloads import unpack
        value, _, turn = self.payload_backup_fixture()
        _, _, _, result = self.backup_fixture()
        manifest = json.loads((Path(result['destination'])/'manifest.json').read_text())
        self.assertTrue(manifest['review_payloads_included'])
        restored = self.operations.restore(self.restore_request(result))
        store = Store(restored['database'])
        same = store.get_object('one', turn['object_id'])
        self.assertEqual(same, turn)
        self.assertEqual(unpack(store, same['body']['request']), value)

    def test_missing_review_payload_prevents_backup_and_incomplete_restore(self):
        _, reference, _ = self.payload_backup_fixture()
        _, _, _, result = self.backup_fixture()
        missing = 'review-payloads/' + reference['manifest_sha256']
        self.replace_backup_manifest(result, lambda manifest, root: manifest['files'].pop(missing))
        with self.assertRaises(DomainError):
            self.operations.restore(self.restore_request(result))
        (self.store.path.parent/missing).unlink()
        with self.assertRaises(DomainError):
            self.operations.backup(BackupRequest(destination=str(self.root/'missing-backup'), media_root=str(self.root/'media')))

    def restore_request(self, result, name='restored'):
        return RestoreRequest(source=result['destination'],destination=str(self.root/name),manifest_sha256=result['manifest_sha256'])

    def replace_backup_manifest(self, result, change):
        root=Path(result['destination'])
        manifest=json.loads((root/'manifest.json').read_text())
        change(manifest,root)
        (root/'manifest.json').write_text(json.dumps(manifest))
        result['manifest_sha256']=hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()

    def test_private_backup_full_restore_retains_history_revokes_access_and_disables_activation(self):
        media,item,issued,result=self.backup_fixture()
        original=self.store.get_object(SYSTEM_PROJECT,issued['credential_id'])
        out=self.operations.restore(self.restore_request(result))
        restored=Store(out['database'])
        self.assertEqual(restored.get_object(SYSTEM_PROJECT,issued['credential_id'],revision=1),original)
        self.assertTrue(restored.get_object(SYSTEM_PROJECT,issued['credential_id'])['body']['revoked'])
        self.assertEqual(MediaStore(restored,out['media_root']).read('one',item['object_id']),media.read('one',item['object_id']))
        self.assertEqual(restored.get_object('one',item['object_id']),item)
        with self.assertRaises(DomainError): AuthService(restored,self.config.public_origin).authenticate(self.token(issued))
        self.auth.authenticate(self.token(issued))  # Source account and credentials unchanged.
        self.assertFalse(out['active'])
        self.assertFalse(out['switch_over_performed'])
        snapshot=json.loads((Path(result['destination'])/'manifest.json').read_text())
        self.assertTrue(snapshot['media_bytes_included'])
        self.assertIn('PRIVATE',snapshot['classification'])
        for path in Path(result['destination']).rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777,0o700 if path.is_dir() else 0o600)

    def test_backup_uses_one_sqlite_snapshot_even_when_live_metadata_changes(self):
        MediaStore(self.store,self.root/'media')
        original_backup=self.store.backup
        def interleave(path):
            original_backup(path)
            self.store.create_object('one','script',{'content':'After snapshot'},'maker',object_id='later')
        with patch.object(self.store,'backup',side_effect=interleave):
            result=self.operations.backup(BackupRequest(destination=str(self.root/'backup'),media_root=str(self.root/'media')))
        restored=Store(self.operations.restore(self.restore_request(result))['database'])
        with self.assertRaises(DomainError): restored.get_object('one','later')
        self.assertEqual(self.store.get_object('one','later')['body']['content'],'After snapshot')

    def test_backup_rejects_missing_media_and_bounds_and_never_overwrites(self):
        media=MediaStore(self.store,self.root/'media')
        item=media.put('one',[b'Original'],'text/plain','maker')
        path=media.path_for('one',item['object_id'])
        path.unlink()
        with self.assertRaises(OSError):
            self.operations.backup(BackupRequest(destination=str(self.root/'backup'),media_root=str(media.root)))
        self.assertFalse((self.root/'backup/manifest.json').exists())
        with self.assertRaises(FileExistsError):
            self.operations.backup(BackupRequest(destination=str(self.root/'backup'),media_root=str(media.root)))
        with self.assertRaises(DomainError):
            self.operations.backup(BackupRequest(destination=str(self.root/'bounded'),media_root=str(media.root),max_total_bytes=1))
        self.assertFalse((self.root/'bounded/manifest.json').exists())

    def hf_take(self, remote, *, settle_unknown=True):
        intent = self.store.create_object('one', 'dispatch-intent', {'operation': 'submit', 'request': {'job_type': 'seedance_2_5'}},
                                          'submission_service')
        self.store.create_object('one', 'job', {'intent': {k: intent[k] for k in ('object_id', 'revision', 'digest')},
                                 'state': 'succeeded', 'remote_job_id': remote}, 'worker_service')
        rid = 'hf-' + remote
        self.store.reserve('one', rid, 120, 'hf_credit', object_id=intent['object_id'], budget_key='hf_owner')
        if settle_unknown:
            self.store.settle('one', rid, None)
        return rid

    def hf_files(self, generations, transactions):
        listing, spends = self.root/'listing.json', self.root/'transactions.json'
        listing.write_text(json.dumps({'items': generations}))
        spends.write_text(json.dumps({'items': transactions}))
        return {'listing_file': str(listing), 'listing_sha256': hashlib.sha256(listing.read_bytes()).hexdigest(),
                'transactions_file': str(spends), 'transactions_sha256': hashlib.sha256(spends.read_bytes()).hexdigest()}

    def test_backup_and_restore_accept_manifest_larger_than_release_file_bound(self):
        # Live store 2026-09-24 outgrew the 4 MiB manifest bound (rehearse-release prepare refused).
        with self.store.transaction() as conn:
            for n in range(24000):
                self.store.create_object('one','note',{'n':n},'maker',conn=conn)
        _,_,_,result=self.backup_fixture()
        size=(Path(result['destination'])/'manifest.json').stat().st_size
        self.assertGreater(size,4*1024*1024)
        restored=Store(self.operations.restore(self.restore_request(result))['database'])
        self.assertEqual(len(restored.list_objects('one')),len(self.store.list_objects('one')))

    def test_backup_manifest_bound_is_still_enforced(self):
        with (patch('production.operations.MAX_BACKUP_MANIFEST_BYTES',1024),
              self.assertRaisesRegex(DomainError,'manifest exceeds')):
            self.backup_fixture()
        self.assertFalse((self.root/'backup/manifest.json').exists())

    def test_restore_refuses_manifest_over_bound_before_parsing(self):
        _,_,_,result=self.backup_fixture()
        with (patch('production.operations.MAX_BACKUP_MANIFEST_BYTES',1024),
              self.assertRaises(DomainError)):
            self.operations.restore(self.restore_request(result))
        self.assertFalse((self.root/'restored').exists())

    def test_restore_hash_corruption_or_missing_blob_does_not_touch_live_database(self):
        _,_,_,result=self.backup_fixture()
        before=self.store.list_objects('one')
        db=Path(result['destination'])/'metadata.sqlite'
        with db.open('ab') as stream: stream.write(b'corrupt')
        with self.assertRaises(DomainError): self.operations.restore(self.restore_request(result))
        self.assertEqual(self.store.list_objects('one'),before)
        self.assertFalse((self.root/'restored/media').exists())
        with self.assertRaises(FileExistsError): self.operations.restore(self.restore_request(result,name='.'))

    def test_restore_rejects_schema_upgrade_even_with_matching_transport_hash(self):
        _,_,_,result=self.backup_fixture()
        def modify(manifest,root):
            path=root/'metadata.sqlite'
            conn=sqlite3.connect(path)
            conn.execute('PRAGMA user_version=999')
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.close()
            manifest['files']['metadata.sqlite']={'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        self.replace_backup_manifest(result,modify)
        with self.assertRaises(DomainError): self.operations.restore(self.restore_request(result))
        self.assertEqual(self.store.get_object('one','one')['object_id'],'one')

    def test_restore_requires_complete_inventory_private_files_and_no_symlinks(self):
        _,_,_,result=self.backup_fixture()
        manifest=Path(result['destination'])/'manifest.json'
        manifest.chmod(0o644)
        with self.assertRaises(DomainError): self.operations.restore(self.restore_request(result))
        manifest.chmod(0o600)
        self.replace_backup_manifest(result,lambda doc,root:doc['files'].pop(next(k for k in doc['files'] if k.startswith('media/'))))
        with self.assertRaises(DomainError): self.operations.restore(self.restore_request(result))
        self.replace_backup_manifest(result,lambda doc,root:doc['files'].update({'../outside':{'size':1,'sha256':'a'*64}}))
        with self.assertRaises(DomainError): self.operations.restore(self.restore_request(result,name='escape'))
        self.assertFalse((self.root/'escape').exists())

    def test_restore_rejects_symlinked_media_and_archive_bound_to_wrong_bytes(self):
        _,_,_,result=self.backup_fixture()
        root=Path(result['destination'])
        blob=next(path for path in (root/'media').rglob('*') if path.is_file())
        raw=blob.read_bytes()
        outside=self.root/'outside'
        outside.write_bytes(raw)
        blob.unlink()
        blob.symlink_to(outside)
        with self.assertRaises(DomainError): self.operations.restore(self.restore_request(result,name='symlink-restore'))
        self.assertFalse((self.root/'symlink-restore').exists())
        blob.unlink()
        blob.write_bytes(raw)
        blob.chmod(0o600)
        def change(manifest,source):
            # Any archived file rewritten with a matching manifest entry: the request's manifest hash no longer fits.
            name=next(k for k in manifest['files'] if k.startswith('media/'))
            (source/name).write_bytes(b'unauthorized different archived bytes\n')
            manifest['files'][name]={'size':(source/name).stat().st_size,'sha256':hashlib.sha256((source/name).read_bytes()).hexdigest()}
        self.replace_backup_manifest(result,change)
        with self.assertRaises(DomainError): self.operations.restore(self.restore_request(result))
        self.assertEqual(outside.read_bytes(),raw)

    def test_backup_restore_cli_and_preexisting_destination_never_overwritten(self):
        media=MediaStore(self.store,self.root/'media')
        media.put('one',[b'History'],'text/plain','maker')
        request=self.root/'backup-request.json'
        request.write_text(BackupRequest(destination=str(self.root/'backup'),media_root=str(media.root)).model_dump_json())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--config',str(self.config_path),'backup','--input',str(request)]),0)
        backup=json.loads(out.getvalue())
        request.write_text(self.restore_request(backup).model_dump_json())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['--config',str(self.config_path),'restore','--input',str(request)]),0)
        self.assertFalse(json.loads(out.getvalue())['switch_over_performed'])
        existing=(self.root/'restored/metadata.sqlite').read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--config',str(self.config_path),'restore','--input',str(request)]),2)
        self.assertEqual((self.root/'restored/metadata.sqlite').read_bytes(),existing)


class AbandonGenerationTests(unittest.TestCase):
    """Live 2026-09-25: a lost apilio image blocked every later release; the operator closes it, the charge stays."""
    def setUp(self):
        import tempfile
        from production.operations import OperatorConfig, Operations
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / 'secrets').mkdir(mode=0o700)
        self.operations = Operations(OperatorConfig(database=str(root / 'store.sqlite'), public_origin='https://film.example',
                                                    operator_id='local_operator', credential_directory=str(root / 'secrets')))
        self.addCleanup(self.operations.close)
        self.store = self.operations.store
        self.store.create_project('one', {}, 'fixture')
        self.store.set_budget('one', 1000000, 'apilio_quota', budget_key='apilio_owner_10466')

    def lost(self, job_type='apilio_gpt_image_2_5', **job):
        ref = lambda o: {k: o[k] for k in ('object_id', 'revision', 'digest')}
        intent = self.store.create_object('one', 'dispatch-intent', {'operation': 'submit', 'request': {'job_type': job_type}},
                                          'submission_service')
        self.store.reserve('one', intent['object_id'], 60000, 'apilio_quota', budget_key='apilio_owner_10466', object_id=intent['object_id'])
        self.store.settle('one', intent['object_id'], None)
        return self.store.create_object('one', 'job', {'state': 'unknown', 'intent': ref(intent), 'reservation_id': intent['object_id'],
                                                       'lease': None, **job}, 'worker_service')

    def request(self, job, key='a1'):
        from production.operations import AbandonGenerationRequest
        return AbandonGenerationRequest(idempotency_key=key, project_id='one', job=ObjectRef(**{k: job[k] for k in ('object_id', 'revision', 'digest')}),
                                        acknowledge_unresolved_charge=True, reason='apilio call passed the 180 s wait')

    def test_a_lost_image_is_closed_and_its_charge_stays_held(self):
        job = self.lost()
        before = self.store.budget('one', budget_key='apilio_owner_10466')
        result = self.operations.abandon_generation(self.request(job))
        self.assertEqual((result['state'], result['billing_changed']), ('cancelled', False))
        self.assertEqual(self.store.get_object('one', job['object_id'])['body']['state'], 'cancelled')
        self.assertEqual(self.store.budget('one', budget_key='apilio_owner_10466'), before)
        self.assertEqual(self.operations.abandon_generation(self.request(job)), result)  # replay

    def test_a_fal_job_whose_polls_are_used_up_can_be_closed(self):
        # the worker never polls it again, so only the operator can end it; the hold stays.
        job = self.lost('fal_seedance_2_5', remote_job_id='01a0d8eb-a6e4-7f32-af1a-abfb219665f7', poll_count=20)
        before = self.store.budget('one', budget_key='apilio_owner_10466')
        self.assertEqual(self.operations.abandon_generation(self.request(job))['state'], 'cancelled')
        self.assertEqual(self.store.budget('one', budget_key='apilio_owner_10466'), before)

    def test_a_job_that_can_still_be_polled_or_listed_is_refused(self):
        for job in (self.lost(remote_job_id='01a0d8eb-a6e4-7f32-af1a-abfb219665f7'), self.lost('seedance_2_5')):
            with self.subTest(job=job['body']), self.assertRaises(DomainError):
                self.operations.abandon_generation(self.request(job, key=job['object_id']))
