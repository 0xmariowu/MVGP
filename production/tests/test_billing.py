"""Operator bills retain unknowns and read expanded evidence without writes."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from production.billing import bill
from production.contracts import DomainError
from production.operations import Operations, OperatorConfig, main
from production.record_payloads import MARKER, RecordPayloads
from production.review_payloads import ReviewPayloads
from production.tests.fixtures import Authority, Transport


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        credentials = self.root/'credentials'
        credentials.mkdir(mode=0o700)
        config = OperatorConfig(database=str(self.root/'store.sqlite'), public_origin='https://film.example',
                                operator_id='local_operator', credential_directory=str(credentials))
        self.operations = Operations(config)
        self.addCleanup(self.operations.close)
        self.store = self.operations.store
        self.config_path = self.root/'operator.json'
        self.config_path.write_text(config.model_dump_json())
        self.config_path.chmod(0o600)
        self.store.create_project('film', {}, 'fixture')
        self.store.set_budget('film', 100, 'credit', budget_key='images')
        self.store.set_budget('film', 1000, 'usd_micro', budget_key='reviews')
        self.store.set_budget('film', 50, 'credit', budget_key='unused')
        self.job = self.obj('job', 'job_one', {
            'operation': 'submit', 'purpose': 'generate still', 'candidate': {'object_id': 'candidate_one'},
            'shot': {'object_id': 'shot_one'}, 'batch': {'object_id': 'batch_one'},
            'take_index': 0, 'remote_job_id': 'provider_123',
        })
        self.reserve('settled', self.job, 20)
        self.store.settle('film', 'settled', 12)
        self.task = self.obj('review-task', 'review_one', {
            'purpose': 'preflight', 'role': 'director', 'route_profile_id': 'review_profile',
            'receipt_id': 'receipt_one',
        })
        self.usage = {'input_tokens': 123, 'output_tokens': 45}
        self.obj('review-receipt', 'receipt_one', {'execution': {'usage': self.usage}})
        self.reserve('unknown', self.task, 90, 'reviews', 'usd_micro')
        self.store.settle('film', 'unknown', None)
        self.batch = self.obj('batch', 'batch_one', {'children': [{'job': self.ref(self.job)}, {}]})
        self.reserve('held', self.batch, 30)

    def obj(self, kind, oid, body):
        return self.store.create_object('film', kind, body, 'fixture', object_id=oid)

    @staticmethod
    def ref(obj):
        return {key: obj[key] for key in ('object_id', 'revision', 'digest')}

    def reserve(self, rid, obj, amount, key='images', unit='credit'):
        self.store.reserve('film', rid, amount, unit, budget_key=key, object_id=obj['object_id'] if obj else None)

    def test_states_descriptions_usage_and_totals(self):
        report = bill(self.store, 'film')
        rows = {row['reservation_id']: row for row in report['rows']}
        self.assertEqual(rows['settled']['actual'], 12)
        self.assertEqual(rows['settled']['reserved_max'], 20)
        self.assertEqual(rows['settled']['target'], {'object_id': 'job_one', 'kind': 'job'})
        for text in ('submit', 'generate still', 'candidate_one', 'shot_one', 'batch_one', 'take: 0', 'provider_123'):
            self.assertIn(text, rows['settled']['what'])
        self.assertIsNone(rows['unknown']['actual'])
        self.assertEqual(rows['unknown']['state'], 'unknown')
        self.assertEqual(rows['unknown']['usage'], self.usage)
        for text in ('preflight', 'director', 'review_profile'):
            self.assertIn(text, rows['unknown']['what'])
        self.assertEqual(rows['held']['state'], 'held')
        self.assertIsNone(rows['held']['actual'])
        self.assertIsNone(rows['held']['usage'])
        self.assertIn('2 children', rows['held']['what'])
        self.assertEqual(report['totals']['images'], {
            'unit': 'credit', 'ceiling': 100, 'count': {'held': 1, 'unknown': 0, 'settled': 1},
            'held_reserved_max': 30, 'unknown_reserved_max': 0, 'reserved_max': 30,
            'actual': 12, 'reserved': 30, 'spent': 12,
        })
        self.assertEqual(report['totals']['reviews']['unknown_reserved_max'], 90)
        self.assertEqual(report['totals']['reviews']['count'], {'held': 0, 'unknown': 1, 'settled': 0})
        self.assertEqual(report['totals']['reviews']['actual'], 0)
        self.assertEqual(report['totals']['unused']['count'], {'held': 0, 'unknown': 0, 'settled': 0})
        for key, total in report['totals'].items():
            budget = self.store.budget('film', budget_key=key)
            for field in ('unit', 'ceiling', 'reserved', 'spent'):
                self.assertEqual(total[field], budget[field])

    def test_real_dispatch_intent_job_and_provider_receipt(self):
        intent = self.obj('dispatch-intent', 'intent_one', {
            'operation': 'submit', 'task': 'hero', 'candidate': {'object_id': 'candidate_two'},
            'target': {'object_id': 'shot_two'}, 'cost': {'reservation': 10},
        })
        receipt = self.obj('provider-receipt', 'provider_receipt', {'settled_cost': 0})
        job = self.obj('job', 'job_two', {'intent': self.ref(intent), 'remote_job_id': 'remote_two',
                                        'last_receipt': self.ref(receipt)})
        self.obj('batch', 'batch_two', {'children': [{'job': self.ref(job)}]})
        self.reserve('real', intent, 10)
        row = next(r for r in bill(self.store, 'film')['rows'] if r['reservation_id'] == 'real')
        for text in ('submit', 'candidate_two', 'shot_two', 'remote_two', 'batch_two'):
            self.assertIn(text, row['what'])
        self.assertEqual(row['usage'], {'settled_cost': 0})
        self.assertIsNone(row['actual'])  # Receipt evidence alone does not settle a reservation.

    def test_external_result_payload_is_decoded(self):
        self.store.record_payloads = RecordPayloads(ReviewPayloads(Transport(Authority())))
        result = self.obj('review-turn', 'turn_one', {'result': {'usage': self.usage, 'text': 'x'*20000}})
        target = self.obj('review-task', 'review_external', {'result': self.ref(result)})
        self.reserve('external', target, 10)
        with self.store.transaction(write=False) as conn:
            physical = conn.execute('SELECT body FROM revisions WHERE object_id=?', ('turn_one',)).fetchone()[0]
            self.assertIn(MARKER, physical)
        # Reopen to avoid creation-time decoded body caches.
        from production.store import Store
        reopened = Store(self.store.path)
        reopened.record_payloads = self.store.record_payloads
        row = next(r for r in bill(reopened, 'film')['rows'] if r['reservation_id'] == 'external')
        self.assertEqual(row['usage'], self.usage)

    def test_event_time_order_missing_target_and_project_isolation(self):
        with self.store.transaction() as conn:
            # Events are append-only; insert an older fixture event, never rewrite one.
            conn.execute('INSERT INTO events(project_id,kind,body,created_at) VALUES (?,?,?,?)',
                         ('film', 'job.queued', json.dumps({'job': self.ref(self.job)}), '2001-01-01T00:00:00.000Z'))
        self.reserve('no_target', None, 1)
        self.store.create_project('other', {}, 'fixture')
        self.store.set_budget('other', 10, 'credit')
        self.store.reserve('other', 'foreign', 2, 'credit')
        rows = bill(self.store, 'film')['rows']
        self.assertEqual(rows[0]['reservation_id'], 'settled')
        self.assertEqual(datetime.fromisoformat(rows[0]['time']).year, 2001)
        self.assertEqual(rows[-1]['reservation_id'], 'no_target')
        self.assertIsNone(rows[-1]['time'])
        self.assertNotIn('foreign', [r['reservation_id'] for r in rows])
        created = next(e['created_at'] for e in self.store.events('film')
                       if e['body'].get('object_id') == 'review_one')
        review = next(r for r in rows if r['reservation_id'] == 'unknown')
        self.assertEqual(datetime.fromisoformat(review['time']), datetime.fromisoformat(created))

    def test_read_only_snapshot_and_no_writes(self):
        before = self.store.events('film')
        with self.store.transaction(write=False) as conn:
            changes = conn.total_changes
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
            report = bill(self.store, 'film', conn=conn)
            self.assertEqual(conn.total_changes, changes)
        self.assertEqual(report, bill(self.store, 'film'))
        self.assertEqual(self.store.events('film'), before)

    def test_empty_project_and_missing_project(self):
        self.store.create_project('empty', {}, 'fixture')
        self.assertEqual(bill(self.store, 'empty'), {'rows': [], 'totals': {}})
        with self.assertRaises(DomainError):
            bill(self.store, 'absent')

    def test_cli_json_and_table(self):
        before = self.store.events('film')
        for format_name in ('json', 'table'):
            with self.subTest(format=format_name), contextlib.redirect_stdout(io.StringIO()) as output:
                status = main(['--config', str(self.config_path), 'bill', '--project', 'film', '--format', format_name])
                self.assertEqual(status, 0)
                if format_name == 'json':
                    self.assertEqual(json.loads(output.getvalue()), bill(self.store, 'film'))
                else:
                    for text in ('Totals', 'Ledger reserved', 'Ledger spent', 'Unknown max', 'usd_micro', 'null'):
                        self.assertIn(text, output.getvalue())
        self.assertEqual(self.store.events('film'), before)
        with patch('production.billing.Store.transaction', wraps=self.store.transaction) as transaction:
            bill(self.store, 'film')
            transaction.assert_called_once_with(write=False)


if __name__ == '__main__':
    unittest.main()


class LedgerTests(unittest.TestCase):
    """where every cent went, per paid call, in the account's own unit."""
    def setUp(self):
        from production.tests.test_submissions import FalCompletionTests
        FalCompletionTests.setUpClass()
        self.fal = FalCompletionTests()
        self.fal.setUp()
        self.addCleanup(self.fal.doCleanups)

    def test_every_paid_call_is_a_row_with_its_shot_provider_request_and_amount(self):
        from production.billing import ledger
        fal = self.fal
        image = fal.s.service.submit(fal.s.actor, fal.pid, fal.s.request('image-1'))  # the asset image candidate
        fal.pick(fal.takes[0])
        completion = fal.s.service.complete_draft(fal.worker, fal.pid, fal.f.ref(fal.takes[0]))
        fal.finish_completion(completion, 'failed', 0)
        before = len(fal.store.events(fal.pid))
        report = ledger(fal.store, fal.pid)
        self.assertEqual(len(fal.store.events(fal.pid)), before)  # read-only
        rows = report['rows']
        self.assertEqual(sorted(r['what'] for r in rows), ['1080p 正片', '样片', '样片', '素材图'])
        drafts = [r for r in rows if r['what'] == '样片']
        self.assertEqual({(r['shot'], r['provider']) for r in drafts}, {('S02-020A', 'fal')})
        self.assertEqual({r['request_id'] for r in drafts}, {'01a0d7fa-efe3-79b2-b20f-62cc6d49e740', '01a0d7fa-efe3-79b2-b20f-62cc6d49e741'})
        full, = [r for r in rows if r['what'] == '1080p 正片']
        self.assertEqual((full['shot'], full['state'], full['amount']), ('S02-020A', 'settled', 0))  # refused at the queue
        picture, = [r for r in rows if r['what'] == '素材图']
        self.assertEqual((picture['asset'], picture['state'], picture['job']), ('@cook', 'held', image['object_id']))
        self.assertTrue(all(r['unit'] == 'credit' and r['time'] for r in rows))
        self.assertEqual(report['totals']['legacy']['settled'], 0)
        self.assertEqual(report['totals']['legacy']['held'], sum(r['amount'] for r in rows if r['state'] == 'held'))


class ReconcileTests(unittest.TestCase):
    """the platform's settled totals next to fal's and apilio's own records, never corrected."""
    def setUp(self):
        from production.store import Store
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / 'store.sqlite')
        self.store.create_project('film', {}, 'fixture')
        self.store.set_budget('film', 50_000_000, 'usd_micro', budget_key='fal_owner')
        self.store.set_budget('film', 5_000_000, 'apilio_quota', budget_key='apilio_owner')
        self.day = datetime.now(tz=timezone.utc).date().isoformat()
        self.paid('draft', 'submit', 'fal_seedance_2_5', 'fal_owner', 'usd_micro', 6_232_230, 830_962)
        self.paid('full', 'complete-draft', 'fal_seedance_2_5_complete', 'fal_owner', 'usd_micro', 34_489_680, 4_598_624)
        self.paid('lost', 'submit', 'fal_seedance_2_5', 'fal_owner', 'usd_micro', 6_232_230, None)
        self.paid('image', 'submit', 'apilio_gpt_image_2_5', 'apilio_owner', 'apilio_quota', 60_000, 20_000)
        self.ledger = self.root / 'ledger'
        self.ledger.mkdir()

    def paid(self, name, operation, job_type, key, unit, hold, charged):
        intent = self.store.create_object('film', 'dispatch-intent', {'operation': operation, 'request': {'job_type': job_type},
                                          'cost': {'budget_key': key}}, 'submission_service', object_id=f'intent_{name}')
        self.store.reserve('film', f'res_{name}', hold, unit, budget_key=key, object_id=intent['object_id'])
        self.store.settle('film', f'res_{name}', charged)
        self.store.create_object('film', 'job', {'state': 'succeeded' if charged is not None else 'unknown', 'reservation_id': f'res_{name}',
                                 'remote_job_id': f'req_{name}', 'intent': {'object_id': intent['object_id'], 'revision': 1}},
                                 'submission_service', object_id=f'job_{name}')

    def write(self, name, value):
        (self.ledger / name).write_text(json.dumps(value, ensure_ascii=False))

    def test_fal_and_apilio_are_compared_by_day_and_the_difference_is_only_listed(self):
        from production.billing import reconcile
        yesterday = (datetime.fromisoformat(self.day) - timedelta(days=1)).date().isoformat()
        self.write(f'fal-{self.day}.json', {'complete': True, 'results': [
            {'endpoint_id': 'bytedance/seedance-2.5/reference-to-video', 'cost_total': 0.830962, 'currency': 'USD'},
            {'endpoint_id': 'bytedance/seedance-2.5/draft/complete', 'cost_total': 4.598624, 'currency': 'USD'},
            {'endpoint_id': 'fal-ai/flux/dev', 'cost_total': 0.32, 'currency': 'USD'}]})  # another tool on the same account
        self.write(f'apilio-{yesterday}.json', {'total_usage': 1000.0})
        self.write(f'apilio-{self.day}.json', {'total_usage': 1006.0})
        report = reconcile(self.store, ['film'], self.day, self.ledger)
        fal = report['providers']['fal']
        self.assertEqual((fal['platform_settled'], fal['provider'], fal['difference']), (5_429_586, 5_749_586, 320_000))
        self.assertEqual(fal['platform_unknown'], 6_232_230)  # the lost answer is shown, not guessed
        self.assertEqual(fal['by_what']['样片'], {'platform': 830_962, 'provider': 830_962})
        self.assertEqual(fal['by_what']['fal-ai/flux/dev'], {'platform': 0, 'provider': 320_000})
        apilio = report['providers']['apilio']
        self.assertEqual((apilio['platform_settled'], apilio['provider'], apilio['difference']), (4.0, 6.0, 2.0))
        self.assertIn('not documented', apilio['conversion'])
        self.assertEqual(self.store.budget('film', budget_key='fal_owner')['spent'], 5_429_586)  # nothing corrected

    def test_higgsfield_takes_are_matched_to_its_own_spends_and_the_rest_is_listed_apart(self):
        """(owner: "你应该统一记账啊"): Higgsfield names no job in a transaction, so each settled take
        is matched to one spend of the same credits just after it; the owner's own web use is listed, not counted."""
        from production.billing import reconcile
        self.store.set_budget('film', 8000, 'hf_credit', budget_key='hf_owner')
        self.paid('hf1', 'submit', 'seedance_2_5', 'hf_owner', 'hf_credit', 360, 48)
        self.paid('hf2', 'submit', 'seedance_2_5', 'hf_owner', 'hf_credit', 360, 48)
        self.paid('hf3', 'submit', 'seedance_2_5', 'hf_owner', 'hf_credit', 360, 60)  # no spend recorded for it
        soon = (datetime.now(tz=timezone.utc) + timedelta(seconds=20)).isoformat().replace('+00:00', 'Z')
        self.write(f'higgsfield-{self.day}.json', {'complete': False, 'balance': {'credits': 8000.00}, 'transactions': [
            {'action': 'spend', 'created_at': soon, 'credits': -48, 'display_name': 'Seedance 2.5'},
            {'action': 'spend', 'created_at': soon, 'credits': -48, 'display_name': 'Seedance 2.5'},
            {'action': 'spend', 'created_at': soon, 'credits': -48, 'display_name': 'Wan 3.0 Prime Video'},
            {'action': 'refund', 'created_at': soon, 'credits': 110, 'display_name': 'Seedance 2.0'}]})
        hf = reconcile(self.store, ['film'], self.day, self.ledger)['providers']['higgsfield']
        self.assertEqual((hf['platform_settled'], hf['provider'], hf['matched']), (156, 144, 2))
        self.assertEqual([t['credits'] for t in hf['platform_without_spend']], [60])
        self.assertEqual([(t['credits'], t['model']) for t in hf['not_the_platforms']], [(48, 'Wan 3.0 Prime Video')])
        self.assertEqual(hf['difference'], 144 - 48 - 156)  # the take with no spend shows as −60
        self.assertEqual([t['action'] for t in hf['other_actions']], ['refund'])
        self.assertEqual(hf['balance'], {'credits': 8000.00})
        self.assertEqual(self.store.budget('film', budget_key='hf_owner')['spent'], 156)  # nothing corrected
        self.assertIn('no Higgsfield record', reconcile(self.store, ['film'], '2020-01-01', self.ledger)['providers']['higgsfield']['note'])

    def test_a_missing_snapshot_says_what_is_missing(self):
        from production.billing import reconcile
        self.write(f'fal-{self.day}.json', {'error': 'FAL_AI_ADMIN_TOKEN is not set; fal usage needs an admin key'})
        report = reconcile(self.store, ['film'], self.day, self.ledger)
        self.assertIn('FAL_AI_ADMIN_TOKEN', report['providers']['fal']['note'])
        self.assertIn('needs the running total', report['providers']['apilio']['note'])
