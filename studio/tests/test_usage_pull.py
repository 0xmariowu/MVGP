"""The providers' own usage records become daily snapshots. Responses recorded 2026-09-27."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from studio.usage_pull import APILIO_USAGE, FAL_USAGE, pull

NOW = dt.datetime(2026, 9, 27, 8, 0, tzinfo=dt.timezone.utc)
# fal with the admin key (shape and figures of the 2026-09-27 read; the docs' example has the same fields).
FAL_PAGE_1 = {'time_series': [{'bucket': '2026-09-25T00:00:00+00:00', 'results': [
    {'endpoint_id': 'bytedance/seedance-2.5/reference-to-video', 'unit': '1000 tokens', 'quantity': 3513.9, 'unit_price': 0.0214,
     'percent_discount': None, 'cost_subtotal': 75.19783129, 'cost_discount': 0, 'cost_total': 75.19783129, 'currency': 'USD'}]}],
    'next_cursor': 'Y3Vyc29yLTI=', 'has_more': True}
FAL_PAGE_2 = {'time_series': [{'bucket': '2026-09-25T00:00:00+00:00', 'results': [
    {'endpoint_id': 'bytedance/seedance-2.5/draft/complete', 'unit': '1000 tokens', 'quantity': 644.7, 'unit_price': 0.0214,
     'percent_discount': None, 'cost_subtotal': 13.79587059, 'cost_discount': 0, 'cost_total': 13.79587059, 'currency': 'USD'}]},
    {'bucket': '2026-09-26T00:00:00+00:00', 'results': []}], 'next_cursor': None, 'has_more': False}
FAL_REFUSED = {'error': {'type': 'authorization_error', 'message': 'This API key is not permitted to perform this action.'}}
APILIO = {'object': 'billing_usage', 'total_usage': 1560000.0}


class UsagePullTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.studio = Path(tmp.name)
        self.calls = []

    def transport(self, answers):
        def call(url, headers):
            self.calls.append((url, headers))
            for prefix, answer in answers:  # 'cursor' in a key: the answer for the next page
                base = prefix.replace('?cursor', '')
                if url.startswith(base) and (('cursor=' in url) == prefix.endswith('?cursor')):
                    return answer
            raise AssertionError(url)
        return call

    def snapshot(self, name):
        return json.loads((self.studio / 'state/live/ledger' / name).read_text())

    def test_fal_days_across_pages_and_apilios_running_total(self):
        keys = {'FAL_AI_ADMIN_TOKEN': 'fal-admin-secret', 'APILIO_AI_KEY': 'apilio-secret'}
        answers = [(FAL_USAGE + '?cursor', (200, json.dumps(FAL_PAGE_2).encode())), (FAL_USAGE, (200, json.dumps(FAL_PAGE_1).encode())),
                   (APILIO_USAGE, (200, json.dumps(APILIO).encode()))]
        written = pull(self.studio, keys, days=2, transport=self.transport(answers), now=NOW)
        self.assertEqual(sorted(p.name for p in written),
                         ['apilio-2026-09-27.json', 'fal-2026-09-25.json', 'fal-2026-09-26.json', 'fal-2026-09-27.json',
                          'higgsfield-2026-09-25.json', 'higgsfield-2026-09-26.json', 'higgsfield-2026-09-27.json'])
        day = self.snapshot('fal-2026-09-25.json')
        self.assertEqual([(r['endpoint_id'], r['cost_total'], r['currency']) for r in day['results']],
                         [('bytedance/seedance-2.5/reference-to-video', 75.19783129, 'USD'),
                          ('bytedance/seedance-2.5/draft/complete', 13.79587059, 'USD')])
        self.assertTrue(day['complete'])
        self.assertFalse(self.snapshot('fal-2026-09-27.json')['complete'])  # today is not over
        self.assertEqual(self.snapshot('apilio-2026-09-27.json')['total_usage'], 1560000.0)
        self.assertEqual(self.snapshot('apilio-2026-09-27.json')['unit'], '人民币分')
        self.assertIn('cursor=Y3Vyc29yLTI%3D', self.calls[1][0])
        self.assertEqual(self.calls[0][1], {'Authorization': 'Key fal-admin-secret'})
        self.assertEqual(self.calls[-1][1], {'Authorization': 'Bearer apilio-secret'})
        for path in written:  # keys never reach a snapshot; files are private
            self.assertNotIn('secret', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.studio / 'state/live/ledger').stat().st_mode & 0o777, 0o700)

    def test_a_refused_or_missing_key_is_written_as_the_error_never_hidden(self):
        answers = [(FAL_USAGE, (403, json.dumps(FAL_REFUSED).encode())), (APILIO_USAGE, (401, b'{"error": {"message": "bad key"}}'))]
        pull(self.studio, {'FAL_AI_ADMIN_TOKEN': 'k', 'APILIO_AI_KEY': 'k'}, days=1, transport=self.transport(answers), now=NOW)
        self.assertEqual(self.snapshot('fal-2026-09-26.json')['error'], 'HTTP 403: This API key is not permitted to perform this action.')
        self.assertEqual(self.snapshot('apilio-2026-09-27.json')['error'], 'HTTP 401: bad key')
        self.calls.clear()
        pull(self.studio, {}, days=1, transport=self.transport([]), now=NOW)
        self.assertEqual(self.calls, [])  # nothing is sent without a key
        self.assertIn('FAL_AI_ADMIN_TOKEN is not set', self.snapshot('fal-2026-09-26.json')['error'])
        self.assertIn('APILIO_AI_KEY is not set', self.snapshot('apilio-2026-09-27.json')['error'])


    def test_a_redirect_is_never_followed_with_the_key(self):
        import http.server
        import threading

        from studio import usage_pull
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('Authorization')))
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/elsewhere')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        status, _ = usage_pull._http(f'http://127.0.0.1:{server.server_port}/usage', {'Authorization': 'Key secret'})
        self.assertEqual(status, 302)
        self.assertEqual([p for p, _ in seen], ['/usage'])  # the key went to one address only


if __name__ == '__main__':
    unittest.main()


# Higgsfield `account transactions --json` (shape of the 2026-09-27 read: no job id; credits negative for a spend).
HF_PAGE_1 = {'cursor': '2', 'items': [
    {'action': 'spend', 'created_at': '2026-09-27T07:10:55.682957Z', 'credits': -48, 'display_name': 'Seedance 2.5'},
    {'action': 'spend', 'created_at': '2026-09-26T13:10:46.582046Z', 'credits': -48, 'display_name': 'Seedance 2.5'}]}
HF_PAGE_2 = {'cursor': '4', 'items': [
    {'action': 'spend', 'created_at': '2026-09-24T13:06:58.587352Z', 'credits': -110, 'display_name': 'Seedance 2.0'},
    {'action': 'spend', 'created_at': '2026-09-10T13:02:43.930783Z', 'credits': -48, 'display_name': 'Wan 3.0 Prime Video'}]}


class HiggsfieldUsageTests(unittest.TestCase):
    """Higgsfield's own transactions, read through the pinned CLI, per UTC day."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.studio = Path(tmp.name)
        self.calls = []

    def run_cli(self, args):
        self.calls.append(args)
        if args[:2] == ['account', 'status']:
            return 0, json.dumps({'credits': 8000.00, 'subscription_plan_type': 'ultra'}).encode()
        page = HF_PAGE_2 if '--cursor' in args else HF_PAGE_1
        return 0, json.dumps(page).encode()

    def snapshot(self, name):
        return json.loads((self.studio / 'state/live/ledger' / name).read_text())

    def test_transactions_land_on_their_day_with_todays_balance(self):
        pull(self.studio, {}, days=3, transport=lambda url, headers: (200, b'{}'), now=NOW, hf_run=self.run_cli)
        today, before = self.snapshot('higgsfield-2026-09-27.json'), self.snapshot('higgsfield-2026-09-26.json')
        self.assertEqual([t['credits'] for t in today['transactions']], [-48])
        self.assertEqual(today['balance'], {'credits': 8000.00})
        self.assertFalse(today['complete'])
        self.assertEqual([t['display_name'] for t in before['transactions']], ['Seedance 2.5'])
        self.assertTrue(before['complete'])
        self.assertNotIn('balance', before)
        self.assertEqual(self.snapshot('higgsfield-2026-09-24.json')['transactions'][0]['credits'], -110)
        # The second page reached a day before the window, so paging stopped there.
        self.assertEqual([a[:2] for a in self.calls], [['account', 'transactions'], ['account', 'transactions'], ['account', 'status']])

    def test_no_cli_or_a_failing_cli_is_written_as_the_error(self):
        pull(self.studio, {}, days=1, transport=lambda url, headers: (200, b'{}'), now=NOW)
        self.assertIn('no Higgsfield CLI', self.snapshot('higgsfield-2026-09-27.json')['error'])
        pull(self.studio, {}, days=1, transport=lambda url, headers: (200, b'{}'), now=NOW, hf_run=lambda args: (1, b''))
        self.assertIn('exited 1', self.snapshot('higgsfield-2026-09-27.json')['error'])
