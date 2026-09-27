"""Bounded private HTTP process lifetime, tested without provider traffic."""
import io
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from production import review_http


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.headers = {'Content-Type':'application/json', 'Accept-Encoding':'identity',
                        'Authorization':'Bearer private-test-key'}
        self.endpoint = 'https://api.deepseek.com/chat/completions'

    def helper(self, code):
        path = self.root/'helper.py'
        path.write_text(code)
        return [sys.executable, '-I', str(path)]

    def pump(self, code, *, data=b'input', timeout=0.3, maximum=1024):
        return review_http._pump(self.helper(code), data, timeout, maximum)

    def test_all_live_profile_endpoints_are_allowlisted(self):
        from production.runtime_config import RuntimeConfig
        catalog = RuntimeConfig.load().section('review_routes')  # the reader's live profile
        live_profiles = {name: profile for name, profile in catalog['profiles'].items()
                         if profile['enablement']['live_enabled']}
        self.assertTrue(live_profiles)
        for name, profile in live_profiles.items():
            with self.subTest(profile=name):
                self.assertIn(profile['endpoint'], review_http.ENDPOINTS)

    def test_unlisted_endpoint_is_refused_before_child_launch(self):
        with patch.object(review_http.subprocess, 'Popen', side_effect=AssertionError('No child launch')) as launch,\
                self.assertRaisesRegex(ValueError, '^Invalid private review HTTP request$'):
            review_http.request('https://api.typesafe.ai/v1/unlisted', b'{}', self.headers, 1, 1024)
        launch.assert_not_called()

    def test_large_material_parent_and_child_share_capacity_without_network(self):
        payload = b'x' * (33 * 1024 * 1024)
        review_http._validate(self.endpoint, payload, self.headers, 120, 1024)
        header = {'endpoint': self.endpoint, 'headers': self.headers,
                  'timeout': 120, 'maximum': 1024, 'size': len(payload)}
        with patch.object(review_http, '_post', return_value=review_http.HTTPResponse(200, b'{}')) as post:
            self.assertEqual(review_http._child(io.BytesIO(review_http._frame(header, payload)), io.BytesIO()), 0)
        self.assertEqual(post.call_args.args[1], payload)
        too_large = review_http.MAX_REQUEST_BYTES + 1
        with patch.object(review_http, '_post', side_effect=AssertionError('No dispatch')):
            self.assertEqual(review_http._child(io.BytesIO(review_http._frame({**header, 'size': too_large}, b'')), io.BytesIO()), 2)
        with self.assertRaises(ValueError):
            review_http._validate(self.endpoint, b'x' * too_large, self.headers, 120, 1024)

    def test_delayed_child_within_budget_returns_and_is_reaped(self):
        result = self.pump('import sys,time\nsys.stdin.buffer.read()\ntime.sleep(0.1)\nsys.stdout.buffer.write(b"ready")\n')
        self.assertEqual(result, b'ready')

    def test_stalled_upload_and_slow_output_are_killed_without_dangling_child(self):
        for code, data in [('import time\ntime.sleep(20)', b'x'*1048576),
                           ('import sys,time\nsys.stdin.buffer.read()\nwhile True:\n sys.stdout.buffer.write(b"x");sys.stdout.buffer.flush();time.sleep(.02)', b'')]:
            created = []
            original = subprocess.Popen
            def capture(*args, factory=original, processes=created, **kwargs):
                process = factory(*args, **kwargs); processes.append(process); return process
            started = time.monotonic()
            with patch.object(review_http.subprocess, 'Popen', side_effect=capture), self.assertRaises(review_http.UnknownOutcome):
                self.pump(code, data=data, timeout=0.15)
            self.assertLess(time.monotonic()-started, 2.5)
            self.assertIsNotNone(created[0].returncode)
            with self.assertRaises(ProcessLookupError): os.kill(created[0].pid, 0)

    def test_actual_child_selector_setup_failure_reaps_or_poison_stops_launches(self):
        for unconfirmed in (False, True):
            with self.subTest(unconfirmed=unconfirmed):
                created = []
                factory = subprocess.Popen
                def capture(*args, unconfirmed=unconfirmed, factory=factory, created=created, **kwargs):
                    process = factory(*args, **kwargs)
                    wait = process.wait
                    created.append((process, wait))
                    if unconfirmed:
                        def failed_wait(timeout=None):
                            # Reap the real fixture first; only exit confirmation
                            # is simulated as missing, never leave a test child.
                            wait(timeout=timeout)
                            raise subprocess.TimeoutExpired('owned-fixture', timeout)
                        process.wait = failed_wait
                    return process
                try:
                    with patch.object(review_http, '_POISONED', False),\
                            patch.object(review_http.subprocess, 'Popen', side_effect=capture),\
                            patch.object(review_http.selectors, 'DefaultSelector', side_effect=OSError('setup failed')):
                        expected = review_http.FatalWorkerError if unconfirmed else review_http.UnknownOutcome
                        with self.assertRaises(expected):
                            self.pump('import time; time.sleep(30)', timeout=0.05)
                        process = created[0][0]
                        self.assertIsNotNone(process.returncode)
                        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
                        self.assertEqual(review_http.is_healthy(), not unconfirmed)
                        if unconfirmed:
                            with self.assertRaises(review_http.FatalWorkerError):
                                self.pump('raise AssertionError("No second process")')
                            self.assertEqual(len(created), 1)
                finally:
                    for process, wait in created:
                        if process.returncode is None:
                            try:
                                os.killpg(process.pid, review_http.signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            wait(timeout=2)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            stream.close()

    def test_secondary_close_failure_cannot_mask_fatal_process_cleanup(self):
        process, selector = MagicMock(), MagicMock()
        process.pid = 123456
        process.wait.side_effect = subprocess.TimeoutExpired('owned-fixture', 2)
        selector.close.side_effect = OSError('selector close failed')
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.side_effect = OSError('pipe close failed')
        with patch.object(review_http, '_POISONED', False),\
                patch.object(review_http.subprocess, 'Popen', return_value=process),\
                patch.object(review_http.selectors, 'DefaultSelector', return_value=selector),\
                patch.object(review_http.os, 'set_blocking', side_effect=OSError('setup failed')),\
                patch.object(review_http.os, 'killpg'):
            with self.assertRaises(review_http.FatalWorkerError):
                review_http._pump(['fixed-fixture'], b'input', 1, 1024)
            self.assertFalse(review_http.is_healthy())
        selector.close.assert_called_once()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once()

    def test_oversized_output_and_private_stderr_fail_without_leaking(self):
        for code in ['import sys\nsys.stdout.buffer.write(b"x"*4096)',
                     'import sys\nsys.stderr.write("private-test-key");sys.exit(9)']:
            with self.assertRaises(review_http.UnknownOutcome) as error:
                self.pump(code, maximum=32)
            self.assertNotIn('private-test-key', str(error.exception))

    def test_observable_failures_have_safe_distinct_codes(self):
        cases = [ ('import time; time.sleep(20)', 'wall_deadline'),
                  ('import sys; sys.stdout.buffer.write(b"x"*4096)', 'stdout_limit'),
                  ('import sys; sys.stderr.write("x"*20000)', 'stderr_limit'),
                  ('import sys; sys.stderr.write("private-test-key"); sys.exit(9)', 'child_exit') ]
        for script, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(review_http.UnknownOutcome) as caught:
                self.pump(script, data=b'', timeout=0.2, maximum=32)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn('private-test-key', str(caught.exception))

    def test_child_network_reason_survives_without_exception_text(self):
        header = {'endpoint': self.endpoint, 'headers': self.headers, 'timeout': 1, 'maximum': 1024, 'size': 2}
        for exception, reason in [(httpx.ReadTimeout('private-test-key'), 'read_timeout'),
                                  (httpx.ConnectError('private-test-key'), 'connect_error')]:
            stdout = io.BytesIO()
            with patch.object(review_http, '_post', side_effect=exception):
                self.assertEqual(review_http._child(io.BytesIO(review_http._frame(header, b'{}')), stdout), 0)
            self.assertNotIn(b'private-test-key', stdout.getvalue())
            with patch.object(review_http, '_pump', return_value=stdout.getvalue()), self.assertRaises(review_http.UnknownOutcome) as caught:
                review_http.request(self.endpoint, b'{}', self.headers, 1, 1024)
            self.assertEqual(caught.exception.reason, reason)

    def test_malformed_or_untrusted_error_frame_is_not_a_diagnostic(self):
        for raw in [b'junk', review_http._frame({'error': 'private-test-key'}, b''),
                    review_http._frame({'error': 'read_timeout'}, b'private-test-key')]:
            with patch.object(review_http, '_pump', return_value=raw), self.assertRaises(review_http.UnknownOutcome) as caught:
                review_http.request(self.endpoint, b'{}', self.headers, 1, 1024)
            self.assertEqual(caught.exception.reason, 'invalid_frame')
            self.assertNotIn('private-test-key', str(caught.exception))

    def test_request_uses_fixed_isolated_interpreter_and_no_secret_environment(self):
        response = review_http._frame({'status_code':200, 'size':2}, b'{}')
        with patch.object(review_http, '_pump', return_value=response) as pump:
            value = review_http.request(self.endpoint, b'{"model":"x"}', self.headers, 120, 1024)
        self.assertEqual((value.status_code, value.body), (200,b'{}'))
        argv, data, timeout, bound = pump.call_args.args
        self.assertEqual(argv, [sys.executable, '-I', str(Path(review_http.__file__).resolve()), '--child'])
        self.assertNotIn('private-test-key', str(argv))
        self.assertIn(b'private-test-key', data)
        self.assertEqual(timeout, 120)
        self.assertLessEqual(bound, 1024+review_http.MAX_HEADER_BYTES+4)
        created = []
        original = subprocess.Popen
        def capture(*args, **kwargs):
            created.append(kwargs)
            return original(*args, **kwargs)
        with patch.object(review_http.subprocess, 'Popen', side_effect=capture):
            self.assertEqual(self.pump('import os,sys\nsys.stdout.buffer.write(str(any(k in os.environ for k in ("DEEPSEEK_API_KEY","APILIO_AI_KEY","PYTHONPATH","HOME","HTTPS_PROXY"))).encode())'), b'False')
        self.assertEqual(created[0]['env'], {})
        self.assertTrue(created[0]['start_new_session'])
        self.assertFalse(created[0].get('shell', False))

    def test_endpoint_headers_limits_and_response_frames_fail_closed(self):
        for endpoint in [self.endpoint+'?key=private-test-key', 'http://127.0.0.1', 'https://evil.example']:
            with self.assertRaises(ValueError), patch.object(review_http, '_pump', side_effect=AssertionError('IO')):
                review_http.request(endpoint, b'{}', self.headers, 1, 1024)
        for timeout in [True, 0, 121, float('nan')]:
            with self.assertRaises(ValueError): review_http.request(self.endpoint, b'{}', self.headers, timeout, 1024)
        for headers in [{**self.headers, 'Proxy-Authorization':'x'}, {**self.headers, 'Authorization':'Bearer bad\nkey'}]:
            with self.assertRaises(ValueError): review_http.request(self.endpoint, b'{}', headers, 1, 1024)
        for raw in [b'junk', struct.pack('!I', 999999), review_http._frame({'status_code':200,'size':3}, b'{}'),
                    review_http._frame({'status_code':200,'size':2,'extra':'x'}, b'{}')]:
            with patch.object(review_http, '_pump', return_value=raw), self.assertRaises(review_http.UnknownOutcome):
                review_http.request(self.endpoint, b'{}', self.headers, 1, 1024)

    def test_child_http_is_one_post_no_redirect_proxy_retry_or_compression(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(307, content=b'redirect', headers={'Location':'https://evil.example'})
        with patch.object(httpx, 'HTTPTransport', return_value=httpx.MockTransport(handle)) as factory:
            response = review_http._post(self.endpoint, b'{}', self.headers, 120, 1024)
        factory.assert_called_once_with(retries=0, trust_env=False)
        self.assertEqual((response.status_code,response.body), (307,b'redirect'))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, 'POST')
        self.assertEqual(requests[0].extensions['timeout']['read'], 120)
        for headers, content in [({'content-encoding':'gzip'}, b'x'), ({}, b'x'*1025)]:
            with patch.object(httpx, 'HTTPTransport', return_value=httpx.MockTransport(lambda req, data=content, fields=headers:httpx.Response(200,content=data,headers=fields))), self.assertRaises(review_http.UnknownOutcome):
                review_http._post(self.endpoint,b'{}',self.headers,120,1024)

    def test_only_exact_native_reader_endpoint_joins_fixed_http_allowlist(self):
        endpoint = 'https://api.apilio.ai/v1beta/models/gemini-3.8-flash:generateContent'
        self.assertEqual(review_http.ENDPOINTS, {self.endpoint,
            'https://api.apilio.ai/v1/chat/completions',
            'https://api.typesafe.ai/v1/systemone', endpoint})
        payload = b'{"contents":[]}'
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, content=b'{"native":true}')
        with patch.object(httpx, 'HTTPTransport', return_value=httpx.MockTransport(handle)):
            response = review_http._post(endpoint, payload, self.headers, 1, 1024)
        self.assertEqual(response.body, b'{"native":true}')
        self.assertEqual(len(requests), 1)
        self.assertEqual((str(requests[0].url), requests[0].content), (endpoint, payload))
        for invalid in [endpoint + '?key=secret', endpoint.replace('3.8', '3.7'),
                        endpoint.replace('generateContent', 'streamGenerateContent')]:
            with patch.object(review_http, '_pump', side_effect=AssertionError('No IO')), self.assertRaises(ValueError):
                review_http.request(invalid, payload, self.headers, 1, 1024)

    def test_public_owned_process_reaper_kills_before_wait_and_has_shared_poison(self):
        process = MagicMock()
        process.pid = 123456
        order = []
        process.wait.side_effect = lambda **kwargs: order.append(('wait', kwargs['timeout']))
        with patch.object(review_http, '_POISONED', False),\
                patch.object(review_http.os, 'killpg', side_effect=lambda *args: order.append(('kill', args))):
            review_http.terminate_owned_process(process)
            self.assertEqual(order[0], ('kill', (process.pid, review_http.signal.SIGKILL)))
            self.assertEqual(order[1][0], 'wait')
            self.assertTrue(0 < order[1][1] <= 2)
            self.assertTrue(review_http.is_healthy())
            process.wait.side_effect = subprocess.TimeoutExpired('private-test-key', 2)
            with self.assertRaises(review_http.FatalWorkerError):
                review_http.terminate_owned_process(process)
            self.assertFalse(review_http.is_healthy())
            with self.assertRaises(review_http.FatalWorkerError):
                review_http.assert_healthy()

    def test_fatal_unreaped_child_poison_prevents_any_further_launch(self):
        process = MagicMock()
        process.pid = 123456
        process.wait.side_effect = subprocess.TimeoutExpired('private-test-key', 2)
        with patch.object(review_http, '_POISONED', False), patch.object(review_http.os, 'killpg'),\
                patch.object(review_http.subprocess, 'Popen', side_effect=AssertionError('No further launch')):
            with self.assertRaises(review_http.FatalWorkerError) as error:
                review_http._reap(process)
            self.assertNotIn('private-test-key', str(error.exception))
            self.assertFalse(review_http.is_healthy())
            self.assertLessEqual(process.wait.call_args.kwargs['timeout'], 2)
            with self.assertRaises(review_http.FatalWorkerError):
                review_http.request(self.endpoint, b'{}', self.headers, 1, 1024)
        self.assertTrue(review_http.is_healthy())
        self.assertFalse(issubclass(review_http.FatalWorkerError, Exception))

    def test_actual_fixed_isolated_child_rejects_invalid_frame_without_http(self):
        argv = [sys.executable, '-I', str(Path(review_http.__file__).resolve()), '--child']
        bad = review_http._frame({'endpoint':'https://invalid.example', 'headers':self.headers,
                                  'timeout':1, 'maximum':1024, 'size':2}, b'{}')
        with self.assertRaises(review_http.UnknownOutcome):
            review_http._pump(argv, bad, 2, 1024)
        # Distinguish a correctly imported child rejecting invalid input from
        # an import/startup error that would also appear as UnknownOutcome.
        result = subprocess.run(argv, input=bad, capture_output=True, env={}, timeout=2, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((result.stdout, result.stderr), (b'', b''))

    def test_child_input_is_bounded_duplicate_keys_rejected_without_network(self):
        for data in [struct.pack('!I',review_http.MAX_HEADER_BYTES+1),
                     struct.pack('!I',13)+b'{"x":1,"x":2}', b'broken']:
            with patch.object(review_http, '_post', side_effect=AssertionError('Network forbidden')):
                self.assertNotEqual(review_http._child(io.BytesIO(data),io.BytesIO()), 0)


if __name__ == '__main__': unittest.main()
