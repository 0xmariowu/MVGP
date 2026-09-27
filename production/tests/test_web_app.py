"""Real Chromium + real API fixtures; no provider calls or production controls."""
import json
import re
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect, sync_playwright

from production.api import create_app
from production.contracts import DecisionRequest, ObjectRef
from production.tests import test_api
from production.tests.fixtures import owner_jwt, owner_login, owner_session

ROOT = Path(__file__).resolve().parents[1]


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_api.MutationAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.pid, self.store, self.auth = f.pid, f.store, f.auth
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.origin = f'https://127.0.0.1:{self.port}'
        self.auth.public_origin = self.origin
        self.app = create_app(f.services, owner_login=owner_login(self.auth))
        self.app.mount('/viewer/fonts', StaticFiles(directory=ROOT.parent / 'production/web/fonts'))
        self.app.mount('/viewer', StaticFiles(directory=ROOT / 'web'))
        def shell():
            return FileResponse(ROOT / 'web/index.html')
        self.app.add_api_route('/projects', shell, methods=['GET'])
        self.app.add_api_route('/projects/{path:path}', shell, methods=['GET'])
        cert, key = f.c.root / 'viewer-cert.pem', f.c.root / 'viewer-key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        config = uvicorn.Config(self.app, host='127.0.0.1', port=self.port, log_level='error',
                                ssl_keyfile=str(key), ssl_certfile=str(cert))
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.sock]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.server.started)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.browser_context = self.browser.new_context(ignore_https_errors=True, viewport={'width': 1200, 'height': 900})
        self.page = self.browser_context.new_page()
        self.requests = []
        self.errors = []
        self.page.on('request', lambda r: self.requests.append((r.method, r.url.removeprefix(self.origin))))
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(10)
        self.sock.close()

    def login(self, kind='viewer', path='/projects', project_ids=None):
        project_ids = project_ids or [self.pid]
        if kind == 'human':
            # the owner signs in through Cloudflare Access, as in production.
            self.owner_signed_in()
            self.page.goto(self.origin + path)
            self.page.locator('#employee-login').click()
        else:
            secret = self.auth.provision_token('screening_viewer', 'viewer', project_ids, 300)
            self.page.goto(self.origin + path)
            expect(self.page.locator('#login')).to_be_visible()
            self.page.get_by_text('管理员提供的访问凭证', exact=True).click()
            self.page.locator('#session-kind').select_option(kind)
            self.page.locator('#session-secret').fill(secret)
            self.page.locator('#login-form button').click()
        expect(self.page.locator('#login')).to_be_hidden()
        if kind != 'human':
            expect(self.page.locator('#session-secret')).to_have_value('')
        self.assertEqual(self.page.evaluate('Object.keys(localStorage).length + Object.keys(sessionStorage).length'), 0)
        self.assertEqual(self.page.evaluate('document.cookie'), '')  # Session is HttpOnly.

    def owner_signed_in(self):
        """Cloudflare Access puts the owner's signed identity on every request (the test key signs it here)."""
        self.page.set_extra_http_headers({'cf-access-jwt-assertion': owner_jwt()})

    def object_url(self, obj, revision=1):
        return f'/projects/{self.pid}/objects/{obj["object_id"]}?revision={revision}'

    def test_history_is_collapsed_lazy_and_still_navigable_at_both_widths(self):
        source = self.store.get_object(self.pid, self.pid)
        new_id = 'project_new_run'
        self.store.create_project(new_id, {**source['body'], 'migration': {
            'source_project': {k: source[k] for k in ('object_id', 'revision', 'digest')},
            'requires_revalidation': True, 'copied_approvals': False}}, 'operator')
        self.login(project_ids=[self.pid, new_id])
        expect(self.page.locator('#project-list .project-card')).to_have_count(1)
        expect(self.page.locator('#project-list')).to_contain_text('当前制作')
        expect(self.page.locator('#project-history-list .project-card')).to_be_hidden()
        self.assertNotIn(('GET', f'/v1/projects/{self.pid}'), self.requests)
        self.page.locator('#project-history summary').click()
        expect(self.page.locator('#project-history-list .project-card')).to_be_visible()
        expect(self.page.locator('#project-history-list')).to_contain_text('历史制作')
        self.page.locator('#project-history-list .project-card').click()
        expect(self.page).to_have_url(self.origin + '/projects/' + self.pid)
        expect(self.page.locator('#project-view')).to_be_visible()
        self.page.locator('a.back').click()
        expect(self.page.locator('#project-history-list .project-card')).to_be_hidden()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.locator('#project-history summary').click()
        expect(self.page.locator('#project-history-list .project-card')).to_be_visible()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])

    def test_project_summary_failure_stops_loading_and_open_retries(self):
        for width in (1200, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                url = self.origin + f'/v1/projects/{self.pid}'
                self.page.route(url, lambda route: route.fulfill(
                    status=503, content_type='application/json',
                    body='{"code":"unavailable","message":"Try again"}'))
                if width == 1200:
                    self.login()
                else:
                    self.page.goto(self.origin + '/projects')
                card = self.page.locator('#project-list .project-card')
                expect(card).to_contain_text('进展读取失败')
                expect(card).not_to_contain_text('读取进展中')
                self.assertEqual(self.page.title(), 'MVGP · 拍片工作台')
                self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                self.page.screenshot(path=f'/tmp/mvgp-progress-failure-{width}.png')
                self.page.unroute(url)
                card.click()
                expect(self.page).to_have_url(self.origin + '/projects/' + self.pid)
                expect(self.page.locator('#project-view')).to_be_visible()
                self.page.screenshot(path=f'/tmp/mvgp-progress-recovered-{width}.png')
        self.assertEqual(self.errors, [])

    def test_real_navigation_lazy_tree_history_and_authored_html_is_text(self):
        f = self.fixture
        scene = f.c.draft('scene', 'EP01/scene.md', '<img src=x onerror="window.injected=true"> A cart passes a marker.')
        f.store.append_revision(self.pid, scene['object_id'], 1, {**scene['body'], 'content': 'The cart continues ahead.'}, 'author')
        self.login()
        expect(self.page.locator('.project-card')).to_have_count(1)
        self.page.get_by_role('button', name='已确认', exact=True).click()
        expect(self.page.locator('.project-card')).to_have_count(0)
        self.page.get_by_role('button', name='全部', exact=True).click()
        expect(self.page.locator('.project-card')).to_have_count(1)
        self.page.locator('.project-card').click()
        expect(self.page.locator('#project-view')).to_be_visible()
        self.assertFalse(any('/tree' in url for _, url in self.requests))
        self.page.get_by_role('button', name='计划', exact=True).click()
        self.page.locator('#plan-list .artifact-row').filter(has_text='scene.md').click()
        expect(self.page.locator('#artifact-content')).to_contain_text('The cart continues ahead.')
        self.page.locator('#versions').select_option('1')
        expect(self.page.locator('#artifact-content')).to_contain_text('<img src=x')
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))
        self.assertEqual(self.page.locator('#artifact-content img').count(), 0)
        self.assertIn('revision=1', self.page.url)
        self.page.reload()
        expect(self.page.locator('#artifact-content')).to_contain_text('<img src=x')
        self.page.get_by_role('button', name='文件', exact=True).click()
        directory = self.page.locator('#file-tree button').filter(has_text='EP01')
        expect(directory).to_be_visible()
        self.assertFalse(any('parent=EP01' in url for _, url in self.requests))
        directory.click()
        expect(self.page.locator('#file-tree button').filter(has_text='scene.md')).to_be_visible()
        self.assertTrue(any('parent=EP01' in url for _, url in self.requests))
        self.assertEqual(self.errors, [])
        self.assertEqual({url for method, url in self.requests if method == 'POST'}, {'/v1/session/exchange'})

    def test_actual_video_exact_playback_copy_fallback_and_failed_media_state(self):
        f = self.fixture
        raw = f.c.synthetic_movie('web', 'pass')
        video = f.c.media.put(self.pid, [raw], 'video/mp4', 'importer_service')
        video = self.store.append_revision(self.pid, video['object_id'], 1,
            {**video['body'], 'import_status': 'imported-unverified', 'label': 'final'}, 'importer_service')
        self.login(path=self.object_url(video, 2) + '&seconds=0.5')
        expect(self.page.locator('#video')).to_be_visible()
        self.page.wait_for_function('() => document.getElementById("video").readyState >= 1')
        self.page.wait_for_function('() => document.getElementById("video").currentTime >= 0.49')
        expect(self.page.locator('#artifact-status')).to_contain_text('尚未验证')
        self.assertNotIn('已确认成片', self.page.locator('#artifact-status').inner_text())
        self.page.evaluate('Object.defineProperty(navigator, "clipboard", {value:{writeText:async()=>{throw Error("denied")}}})')
        with self.page.expect_request(lambda r: r.url.endswith('/reference')) as request:
            self.page.locator('#copy-reference').click()
        payload = request.value.post_data_json
        self.assertEqual(payload['target'], {k: video[k] for k in ('object_id', 'revision', 'digest')})
        self.assertAlmostEqual(payload['seconds'], 0.5, places=1)
        expect(self.page.locator('#copy-fallback')).to_be_visible()
        copied = self.page.locator('#copy-text').input_value()
        self.assertIn('revision=2&seconds=', copied)
        self.assertIn('"source_mapping": null', copied)
        self.assertEqual(self.page.locator('#copy-text').evaluate('(n)=>n.selectionEnd-n.selectionStart'), len(copied))
        f.c.media.path_for(self.pid, video['object_id'], revision=2).unlink()
        self.page.reload()
        expect(self.page.locator('#media-error')).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_selected_media_visible_above_long_internal_execution_history(self):
        raw = self.fixture.c.synthetic_movie('viewer-history', 'pass')
        video = self.fixture.c.media.put(self.pid, [raw], 'video/mp4', 'importer_service')
        for number in range(35):
            self.store.create_object(self.pid, 'review-turn', {'status': 'failed', 'number': number},
                                     'review_runner_service')
        self.login(path=self.object_url(video))
        expect(self.page.locator('#video')).to_be_visible()
        self.page.wait_for_function('() => document.getElementById("video").readyState >= 1')
        player = self.page.locator('#video').bounding_box()
        self.assertGreaterEqual(player['y'], 0)
        self.assertLess(player['y'], 900)
        records = self.page.locator('#result-list .execution-records')
        expect(records).to_have_count(1)
        self.assertFalse(records.evaluate('(el) => el.open'))
        expect(records.locator('.artifact-row')).to_have_count(35)
        records.locator('summary').click()
        expect(records.locator('.artifact-row').first).to_be_visible()
        records.locator('.artifact-row').first.click()
        expect(self.page.locator('#artifact-status')).to_contain_text('失败')
        self.page.goto(self.origin + self.object_url(video))
        expect(self.page.locator('#video')).to_be_visible()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.reload()
        expect(self.page.locator('#video')).to_be_visible()
        self.assertLess(self.page.locator('#video').bounding_box()['y'], 844)
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.assertEqual(self.errors, [])

    def test_viewer_cannot_confirm_independent_human_posts_bound_budget_decision(self):
        f = self.fixture
        self.store.set_budget(self.pid, 40, 'credit', budget_key='hf_primary')
        target = self.store.get_object(self.pid, self.pid)
        decision = f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='web-budget', expected_revision=target['revision'],
            target=ObjectRef(**{k: target[k] for k in ('object_id', 'revision', 'digest')}), purpose='envelope',
            rationale='Raise the synthetic provider-native envelope.', proposed_limit=50, budget_unit='credit', budget_key='hf_primary'))
        self.login(path=self.object_url(decision))
        expect(self.page.locator('#decision-view')).to_be_visible()
        expect(self.page.locator('#decision-evidence')).to_contain_text('proposed_limit')
        expect(self.page.locator('#decision-description')).to_contain_text('hf_primary')
        expect(self.page.locator('#decision-description')).to_contain_text('credit')
        expect(self.page.locator('#decision-actions')).to_be_hidden()
        self.assertEqual(self.store.budget(self.pid)['ceiling'], 40)
        self.page.locator('#logout').click()
        expect(self.page.locator('#login')).to_be_visible()
        self.login(kind='human', path=self.object_url(decision))
        expect(self.page.locator('#decision-actions')).to_be_visible()
        decide = f.decisions.decide

        def delayed_decision(*args, **kwargs):
            time.sleep(6)  # A valid response may exceed the 5-second UI assertion default.
            return decide(*args, **kwargs)

        with patch.object(f.decisions, 'decide', side_effect=delayed_decision),\
                self.page.expect_response(lambda r: r.url.endswith('/human-decisions')) as response:
            with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
                self.page.locator('#confirm-decision').click()
            expect(self.page.locator('#confirm-decision')).to_be_disabled()
            expect(self.page.locator('#decline-decision')).to_be_disabled()
            self.assertEqual(self.store.budget(self.pid, budget_key='hf_primary')['ceiling'], 40)
        self.assertEqual(response.value.status, 200)
        payload = request.value.post_data_json
        self.assertEqual(payload['request_id'], decision['object_id'])
        self.assertEqual(payload['target_hash'], target['digest'])
        self.assertTrue(len(payload['csrf_token']) >= 16)
        expect(self.page.locator('#decision-actions')).to_be_hidden()
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_primary')['ceiling'], 50)
        self.assertEqual(self.store.budget(self.pid)['ceiling'], 40)
        self.assertEqual(self.store.list_objects(self.pid, kind='final'), [])
        self.assertEqual(self.errors, [])
        self.assertEqual(f.c.provider_calls, [])
        self.assertEqual(f.c.review_calls, [])

    def test_viewer_sees_takes_independent_human_posts_selected_take(self):
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        shot = self.store.create_object(self.pid, 'shot', {'content': 'Synthetic shot'}, 'author')
        candidate = self.store.create_object(self.pid, 'candidate', {'target': ref(shot),
            'dependencies': [ref(shot)]}, 'compiler_service')
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('take-choice', 'pass')], 'video/mp4', 'worker_service')
        takes = [self.store.create_object(self.pid, 'media', {**raw['body'],
            'dependencies': [ref(candidate)]}, 'worker_service') for _ in range(2)]
        decision = f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='web-take', expected_revision=1,
            target=ref(shot), shot=ref(shot), takes=[ref(take) for take in takes], purpose='take', rationale='Pick a take.'))
        self.login(path=self.object_url(decision))
        expect(self.page.locator('#decision-title')).to_have_text('选一条')
        expect(self.page.locator('#decision-view video')).to_have_count(2)
        expect(self.page.locator('#decision-actions')).to_be_hidden()
        expect(self.page.locator('input:not([type=password])')).to_have_count(0)
        expect(self.page.locator('#decision-view textarea')).to_have_count(0)
        for index, take in enumerate(takes):
            expect(self.page.locator('#decision-view video').nth(index)).to_have_attribute('src',
                f'/v1/projects/{self.pid}/media/{take["object_id"]}?revision=1')
        self.page.locator('#logout').click()
        expect(self.page.locator('#login')).to_be_visible()
        self.login(kind='human', path=self.object_url(decision))
        expect(self.page.locator('#decision-actions')).to_be_visible()
        self.page.locator('#confirm-decision').click()
        expect(self.page.locator('#notice')).to_contain_text('请先选一条')
        self.assertFalse(any(path.endswith('/human-decisions') for _, path in self.requests))
        self.page.get_by_role('radio', name='选这条').nth(1).check()
        self.page.locator('#decision-reason').fill('The movement reads clearly.')
        with self.page.expect_response(lambda r: r.url.endswith('/human-decisions')) as response,\
                self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            self.page.locator('#confirm-decision').click()
        self.assertEqual(response.value.status, 200)
        payload = request.value.post_data_json
        self.assertEqual(payload['selected_take'], ref(takes[1]))
        self.assertEqual(payload['reason'], 'The movement reads clearly.')
        self.assertEqual(payload['request_id'], decision['object_id'])
        self.assertEqual(payload['target_hash'], shot['digest'])
        self.assertTrue(len(payload['csrf_token']) >= 16)
        expect(self.page.locator('#decision-actions')).to_be_hidden(timeout=15000)  # the object page re-reads related records one by one before it redraws
        selection, = self.store.list_objects(self.pid, kind='human-take-selection')
        self.assertEqual(selection['body']['take'], ref(takes[1]))
        self.assertTrue(selection['body']['verified_human_session'])
        second = f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='web-take-decline', expected_revision=1,
            target=ref(shot), shot=ref(shot), takes=[ref(take) for take in takes], purpose='take', rationale='Try another choice.'))
        self.page.locator('#logout').click()
        expect(self.page.locator('#login')).to_be_visible()
        self.login(kind='human', path=self.object_url(second))
        self.page.get_by_role('radio', name='选这条').first.check()
        with self.page.expect_response(lambda r: r.url.endswith('/human-decisions')) as response,\
                self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            self.page.locator('#decline-decision').click()
        self.assertEqual(response.value.status, 200)
        self.assertNotIn('selected_take', request.value.post_data_json)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='human-take-selection')), 1)
        self.assertEqual(self.errors, [])
        self.assertEqual(f.c.provider_calls, [])
        self.assertEqual(f.c.review_calls, [])

    def test_review_desk_records_a_pick_and_a_decline_and_shows_the_cut(self):
        """看片台 (Claude Design v6): hover a take, 用这条 / 都不行 post the human decisions; 看成片 lines up picks."""
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('review-desk', 'pass')], 'video/mp4', 'worker_service')
        requests = {}
        for label, line in (('S01-010A', '我想辞职。'), ('S01-020A', '好。就是这句。')):
            shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': label, 'Audio': {'delivery': [{'line': line}]},
                'The material': {'the action in one to three sentences': f'{label} action.'},
                'Direction': {'the goal of the shot in one line': f'{label} 这颗要看清她下定决心。'}}}, 'author')
            candidate = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
            takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(candidate)]}, 'worker_service')
                     for _ in range(2)]
            decision = f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key=f'desk-{label}', expected_revision=1,
                target=ref(shot), shot=ref(shot), takes=[ref(t) for t in takes], purpose='take',
                rationale=f'第 1 条 {label} 表演偏平；第 2 条节奏对。'))
            requests[label] = (shot, takes, decision)
        # A reshoot in progress (design v6): one take done, one still being made.
        making = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S01-030A',
            'The material': {'the action in one to three sentences': 'S01-030A action.'}}}, 'author')
        made = self.store.create_object(self.pid, 'media', raw['body'], 'worker_service')
        jobs = [self.store.create_object(self.pid, 'job', {'state': state, **({'result': ref(made)} if state == 'succeeded' else {})},
                                         'worker_service') for state in ('succeeded', 'running')]
        self.store.create_object(self.pid, 'batch', {'state': 'running', 'count': 2,
            'children': [{'target': ref(making), 'job': ref(job)} for job in jobs]}, 'batch_service')
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        cards = self.page.locator('.card')
        expect(cards).to_have_count(3)
        expect(self.page.locator('.project .badge-n')).to_have_text('2')
        expect(self.page.locator('#proj-page')).to_have_text('项目页')  # opens in place
        # the desk reads one feed, not the project summary plus one read per record.
        reads = [path for method, path in self.requests if method == 'GET' and path.startswith('/v1/projects/')]
        self.assertIn(f'/v1/projects/{self.pid}/review-feed', reads)
        self.assertNotIn(f'/v1/projects/{self.pid}', reads)
        self.assertEqual([path for path in reads if '/artifacts/' in path], [])
        reshoot = cards.filter(has_text='S01-030A')
        expect(reshoot.locator('.status')).to_have_text('生成中 1/2')
        expect(reshoot.locator('.take.pending')).to_have_count(1)
        expect(reshoot.locator('.take video')).to_have_count(1)
        expect(reshoot.get_by_role('button', name='再拍一批', exact=True)).to_have_count(0)
        first = cards.filter(has_text='S01-010A')
        # Design v6: shot, time, one plain line; no lines, English or notes.
        expect(first.locator('.card-intent')).to_have_text('S01-010A 这颗要看清她下定决心。')
        expect(first.locator('.card-id')).to_contain_text('今天')
        expect(first.locator('.fresh')).to_have_text('刚拍好')
        expect(first).not_to_contain_text('我想辞职。')
        expect(first).not_to_contain_text('S01-010A action.')
        expect(first.locator('.card-advice')).to_have_count(0)
        # Design v6 density: three shot cards fit a 900 px tall screen.
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        self.assertLess(first.bounding_box()['height'], 300)
        expect(first.locator('.status')).to_have_text('待审')
        expect(first.locator('.take video')).to_have_count(2)
        shot, takes, decision = requests['S01-010A']
        first.locator('.take').nth(1).hover()
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            first.get_by_role('button', name='用这条', exact=True).click()
        payload = request.value.post_data_json
        self.assertEqual((payload['choice'], payload['selected_take'], payload['request_id'], payload['target_hash']),
                         ('confirm', ref(takes[1]), decision['object_id'], shot['digest']))
        expect(first.locator('.status')).to_have_text('定了第 2 条')
        selection, = self.store.list_objects(self.pid, kind='human-take-selection')
        self.assertEqual(selection['body']['take'], ref(takes[1]))
        # (design v6): hovering another take re-picks; hovering the picked take shows 取消.
        first.locator('.take').nth(0).hover()
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            first.get_by_role('button', name='用这条', exact=True).click()
        payload = request.value.post_data_json
        self.assertEqual((payload['choice'], payload['selected_take']), ('confirm', ref(takes[0])))
        expect(first.locator('.status')).to_have_text('定了第 1 条')
        # Design v6 footer: 撤销选择 withdraws the pick.
        expect(first.get_by_role('button', name='再拍一批', exact=True)).to_have_count(0)
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            first.get_by_role('button', name='撤销选择', exact=True).click()
        payload = request.value.post_data_json
        self.assertEqual((payload['choice'], payload['reason']), ('decline', '取消'))
        self.assertNotIn('selected_take', payload)
        expect(first.locator('.status')).to_have_text('待审')
        expect(first.locator('.take .tick')).to_have_count(0)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='human-take-selection')), 3)
        first.locator('.take').nth(1).hover()
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')):
            first.get_by_role('button', name='用这条', exact=True).click()
        expect(first.locator('.status')).to_have_text('定了第 2 条')
        second = cards.filter(has_text='S01-020A')
        second.get_by_role('button', name='都不行', exact=True).click()
        second.locator('.note-box input').fill('人物站位不对')
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            second.get_by_role('button', name='记下', exact=True).click()
        payload = request.value.post_data_json
        self.assertEqual((payload['choice'], payload['reason']), ('decline', '都不行：人物站位不对'))
        self.assertNotIn('selected_take', payload)
        expect(second.locator('.status')).to_have_text('都不行')
        expect(self.page.locator('#copy-all')).to_have_text('复制 5 条给 Agent')  # pick, re-pick, 取消, pick, 都不行
        self.page.locator('#toggle-cut').click()
        expect(self.page.locator('#cut-view')).to_be_visible()
        expect(self.page.locator('#cut-strip .seg')).to_have_count(3)
        expect(self.page.locator('#cut-strip .seg.missing')).to_have_count(2)
        expect(self.page.locator('#cut-approve')).to_be_disabled()
        # (owner "在成片的那个片段那里点击换其他的片子看"): a segment lists its takes; trying one only
        # swaps the preview; 用这条 is the only thing that records a pick.
        decisions = lambda: sum(1 for method, path in self.requests if method == 'POST' and path.endswith('/human-decisions'))
        before = decisions()
        self.page.locator('#cut-strip .seg').filter(has_text='S01-010A').click()
        swap = self.page.locator('#cut-takes')
        expect(swap.locator('.l-take')).to_have_count(2)
        expect(swap.locator('.l-take.on')).to_contain_text('第 2 条')
        swap.locator('.l-take').nth(0).click()
        expect(self.page.locator('#cut-ref')).to_have_text('第 1 条 · 试看')
        expect(swap.locator('.l-take.on')).to_contain_text('第 1 条')
        self.assertIn(takes[0]['object_id'], self.page.locator('#cut-video').get_attribute('src'))
        self.assertEqual(decisions(), before)
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            swap.get_by_role('button', name='用这条', exact=True).click()
        self.assertEqual((request.value.post_data_json['choice'], request.value.post_data_json['selected_take']),
                         ('confirm', ref(takes[0])))
        expect(self.page.locator('#cut-ref')).to_have_text('第 1 条')
        expect(swap.get_by_role('button', name='用这条', exact=True)).to_have_count(0)
        self.assertEqual(self.errors, [])
        self.assertEqual(f.c.provider_calls, [])
        self.assertEqual(f.c.review_calls, [])

    def test_review_desk_shows_the_film_being_made_then_the_automatic_film(self):
        """all shots picked → 成片生成中; the platform's film plays with 这版可以; a segment can still be swapped."""
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('auto-film', 'pass')], 'video/mp4', 'worker_service')
        requests = {}
        for label in ('S01-010A', 'S01-020A'):
            shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': label}}, 'author')
            candidate = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
            takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(candidate)]}, 'worker_service')
                     for _ in range(2)]
            requests[label] = (takes, f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key=f'film-{label}',
                expected_revision=1, target=ref(shot), shot=ref(shot), takes=[ref(t) for t in takes], purpose='take', rationale='Pick one.')))
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        cards = self.page.locator('.card')
        expect(cards).to_have_count(2)
        for n in range(2):
            card = cards.nth(n)
            card.locator('.take').nth(0).hover()
            card.get_by_role('button', name='用这条', exact=True).click()
            expect(card.locator('.status')).to_have_text('定了第 1 条')
        self.page.locator('#toggle-cut').click()
        expect(self.page.locator('#cut-status')).to_contain_text('成片生成中')
        expect(self.page.locator('#cut-approve')).to_have_text('成片生成中')
        expect(self.page.locator('#cut-approve')).to_be_disabled()
        # The platform offers its film; the desk shows it on its next read.
        film = self.store.create_object(self.pid, 'media', raw['body'], 'cut_service')
        final = self.store.create_object(self.pid, 'decision-request', {'target': ref(film), 'target_hash': film['digest'],
            'purpose': 'final', 'rationale': '按你选的各条自动拼好的成片。', 'evidence': {}, 'state': 'pending',
            'expires_at': 9e9, 'human_confirmation_available': True, 'dependencies': [ref(film)]}, 'decision_service')
        self.page.locator('#toggle-cut').click()
        self.page.locator('.project').first.click()
        self.page.locator('#toggle-cut').click()
        expect(self.page.locator('#cut-approve')).to_have_text('这版可以')
        expect(self.page.locator('#cut-approve')).to_be_enabled()
        expect(self.page.locator('#cut-intent')).to_contain_text('按你选的各条自动拼好的成片')
        self.assertIn(film['object_id'], self.page.locator('#cut-video').get_attribute('src'))
        # Swapping a segment still works while the film is offered; the film view comes back with 回到成片.
        self.page.locator('#cut-strip .seg').filter(has_text='S01-020A').click()
        expect(self.page.locator('#cut-takes .l-take')).to_have_count(2)
        expect(self.page.locator('#cut-approve')).to_be_disabled()
        self.page.locator('#cut-takes .l-take').nth(1).click()
        expect(self.page.locator('#cut-ref')).to_have_text('第 2 条 · 试看')
        self.page.get_by_role('button', name='回到成片', exact=True).click()
        expect(self.page.locator('#cut-approve')).to_be_enabled()
        self.assertIn(film['object_id'], self.page.locator('#cut-video').get_attribute('src'))
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as request:
            self.page.locator('#cut-approve').click()
        payload = request.value.post_data_json
        self.assertEqual((payload['request_id'], payload['choice']), (final['object_id'], 'confirm'))
        self.assertEqual(self.errors, [])
        # dry run: once the owner has confirmed the film, it stays on screen as confirmed, not as 成片生成中.
        # (This fixture's film is not a rendered cut, so the platform refuses the click above; mark it confirmed here.)
        self.store.append_revision(self.pid, final['object_id'], final['revision'], {**final['body'], 'state': 'confirmed'},
                                   'decision_service')
        self.page.reload()
        self.page.locator('#toggle-cut').click()
        expect(self.page.locator('#cut-approve')).to_have_text('已确认')
        expect(self.page.locator('#cut-approve')).to_be_disabled()
        expect(self.page.locator('#cut-status')).to_contain_text('已经确认')
        self.assertIn(film['object_id'], self.page.locator('#cut-video').get_attribute('src'))

    def test_review_desk_does_not_show_old_shot_plan_requests(self):
        """HF has no shot-plan step; old requests stay in the record only."""
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S03-DOOR'}, 'author')
        # A pending shot-plan request as release 79 left them in the live record.
        self.store.create_object(self.pid, 'decision-request', {'purpose': 'shot-plan', 'state': 'pending',
            'target': ref(scene), 'rationale': 'S03 shot plan.', 'expires_at': 4102444800,
            'evidence': {'shots': [{'shot': 'S03-010A'}]}, 'dependencies': [ref(scene)]}, 'decision_service')
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        expect(self.page.locator('.empty')).to_contain_text('都审完了')
        expect(self.page.locator('.card.plan')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_project_page_is_the_hf_tree_and_opens_items_in_hf_viewer(self):
        """(owner: 完全照 HF，弹大窗口): HF folders, images/videos only,
        an item opens in HF's full-screen viewer."""
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        self.app.add_api_route('/p/{pid}', lambda pid: FileResponse(ROOT / 'web/project.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('project-page', 'pass')], 'video/mp4', 'worker_service')
        self.store.create_object(self.pid, 'script', {'logical_path': 'story/script.md', 'content': 'INT. ROOM\nANN: 等等。'}, 'author')
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S03-DOOR\n'}, 'author')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S03-010A',
            'Direction': {'the goal of the shot in one line': '她在门口停住。'}}, 'dependencies': [ref(scene)]}, 'author')
        from production import prompt as prompts
        written = 'SCENE CONTEXT @ann stops at the door. 50mm. 0-15s.'
        assets = {'ann': {'descriptor': None, 'image': 'img'}}
        sent, _, additions = prompts.build(written, assets, {}, None, 15, 'every')
        candidate = self.store.create_object(self.pid, 'candidate', {'target': ref(shot),
            'request': {'references': [{'n': 1, 'tag': 'ann'}]}, 'compilation': {
            'prompt': sent, 'writer_text': written, 'additions': additions, 'prompt_constants': prompts.constants(assets, {}, 15),
            'job_type': 'seedance_2_5', 'parameters': {'duration': 15},
            'authorship': {'playbook_version': 'pb-0123456789ab', 'change_note': 'Ann stops one step earlier.'}},
            'dependencies': [ref(shot)]}, 'compiler_service')
        intent = self.store.create_object(self.pid, 'dispatch-intent', {'candidate': ref(candidate), 'dependencies': [ref(candidate)]},
                                          'submission_service')
        takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(intent)]}, 'worker_service') for _ in range(2)]
        receipt = self.store.create_object(self.pid, 'human-receipt', {'verified_human_session': True}, 'decision_service')
        self.store.create_object(self.pid, 'human-take-selection', {'shot': ref(shot), 'take': ref(takes[1]),
            'human_receipt': ref(receipt), 'verified_human_session': True}, 'decision_service')
        self.store.create_object(self.pid, 'asset', {'logical_path': 'assets/cast/ann.json', 'content': {'type': 'asset', 'role': 'visual',
            'tag': '@ann', 'category': 'character', 'definition': {'descriptor': 'A woman in a grey coat.'}}}, 'author')
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        before = len(self.requests)
        self.page.goto(self.origin + f'/p/{self.pid}')
        tree = self.page.locator('.hf-folders .tree')
        for name in ('全部素材', '资产', 'S03-DOOR', '测试', '成片'):
            expect(tree.get_by_text(name, exact=True)).to_have_count(1)
        expect(tree.get_by_text('故事', exact=True)).to_have_count(0)
        expect(self.page.locator('.hf-notes summary')).to_have_text(['剧本'])  # text records are notes, not tiles
        tree.get_by_text('资产', exact=True).click()
        tree.get_by_text('角色', exact=True).click()
        expect(self.page.locator('.hf-grid .hf-tile')).to_have_count(1)
        expect(self.page.locator('.hf-grid .hf-tile .hf-fail')).to_have_text('还没有图')
        tree.get_by_text('S03-DOOR', exact=True).click()
        tree.get_by_text('S03-010A', exact=True).click()
        tiles = self.page.locator('.hf-grid .hf-tile')
        expect(tiles).to_have_count(2)
        expect(self.page.locator('.hf-goal')).to_have_text('她在门口停住。')
        expect(tiles.nth(1).locator('.tick')).to_have_count(1)
        tiles.nth(0).click()
        detail = self.page.locator('.hfx')
        expect(detail).to_be_visible()
        self.assertEqual(detail.evaluate('e => getComputedStyle(e).position'), 'fixed')  # HF's full-screen viewer
        box = detail.bounding_box(); size = self.page.viewport_size
        self.assertEqual((box['width'], box['height']), (size['width'], size['height']))
        expect(detail.locator('.hfx__media video')).to_have_count(1)
        expect(detail.locator('.hfx__copy')).to_have_text('复制')
        expect(detail.locator('.hfx__actions a')).to_have_text('下载')
        kv = detail.locator('dl.kv')  # the manuals reached the writer, visibly
        for label, value in (('手册版本', 'pb-0123456789ab'), ('这版改了什么', 'Ann stops one step earlier.')):
            expect(kv.locator('dt', has_text=label)).to_have_count(1)
            expect(kv.locator('dd', has_text=value)).to_have_count(1)
        expect(kv.locator('dt', has_text='写手段落')).to_have_count(0)
        # the writer's text with the platform's additions marked, and the proof line.
        expect(detail.locator('.hfx__prompt pre')).to_have_text(sent)
        expect(detail.locator('.hfx__prompt mark.hfx__add')).to_have_text(['@Image1', 'No music.'])
        expect(detail.locator('.hfx__proof')).to_have_text('发出去的 = 写手原文 + 2 处补充 ✓')
        expect(detail).to_contain_text('seedance_2_5')
        detail.locator('.hfx__speed').select_option('1.5')  # bug hunt r82: the speed select keeps focus; keys still work
        self.page.keyboard.press('ArrowRight')
        expect(detail.locator('.hfx__who .name')).to_have_text('第 2 条')
        expect(detail).to_contain_text('✓ 这一条')
        self.page.keyboard.press('Escape')
        expect(detail).to_be_hidden()
        expect(tiles).to_have_count(2)
        self.assertEqual(self.errors, [])
        self.assertFalse([path for method, path in self.requests[before:] if method != 'GET'])  # the page only reads

    def test_project_page_opens_in_the_desk_main_area_and_the_project_bar_stays(self):
        """(owner: 红色区域展开啊…实在没必要跳出去)."""
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S03-DOOR\n'}, 'author')
        self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S03-010A'}, 'dependencies': [ref(scene)]}, 'author')
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        start = self.page.url
        self.page.locator('#proj-page').click()
        view = self.page.locator('#project-view')
        expect(view).to_be_visible()
        expect(view.locator('.hf-folders .tree').get_by_text('S03-DOOR', exact=True)).to_have_count(1)
        expect(self.page.locator('#feed')).to_be_hidden()
        expect(self.page.locator('.side #projects .project')).to_have_count(1)  # the project bar stays
        self.assertEqual(self.page.url, start)  # no navigation to another page
        expect(self.page.locator('#proj-page')).to_have_text('回到看片')
        self.page.locator('#proj-page').click()
        expect(view).to_be_hidden()
        expect(self.page.locator('#feed')).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_a_reload_keeps_the_owner_in_and_picking_still_works(self):
        """(owner: 登陆过，还反反复复提示，让我登陆)."""
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('reload', 'pass')], 'video/mp4', 'worker_service')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S02-010A'}}, 'author')
        candidate = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(candidate)]}, 'worker_service') for _ in range(2)]
        f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='reload-offer', expected_revision=1, target=ref(shot),
            shot=ref(shot), takes=[ref(t) for t in takes], purpose='take', rationale='2 条拍好了'))
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        self.page.reload()
        expect(self.page.locator('.card').filter(has_text='S02-010A')).to_have_count(1)
        expect(self.page.locator('#login')).to_be_hidden()
        card = self.page.locator('.card').filter(has_text='S02-010A')
        card.locator('.take').nth(0).hover()
        with self.page.expect_response(lambda r: r.url.endswith('/human-decisions')) as response:
            card.get_by_role('button', name='用这条', exact=True).click()
        self.assertEqual(response.value.status, 200)
        expect(card.locator('.status')).to_have_text('定了第 1 条')
        self.assertEqual(self.errors, [])

    def test_a_lapsed_session_renews_one_request_and_never_repeats_a_decision(self):
        """Bug hunt 2026-09-24: one 撤销选择 click must record exactly one withdrawal, even when the
        session lapses between the decision and the feed reload."""
        import sqlite3
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('retry', 'pass')], 'video/mp4', 'worker_service')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S07-010A'}}, 'author')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service') for _ in range(2)]
        f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='retry', expected_revision=1, target=ref(shot),
            shot=ref(shot), takes=[ref(t) for t in takes], purpose='take', rationale='r'))
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        card = self.page.locator('.card').filter(has_text='S07-010A')
        card.locator('.take').nth(0).hover()
        with self.page.expect_response(lambda r: r.url.endswith('/human-decisions')):
            card.get_by_role('button', name='用这条', exact=True).click()
        expect(card.locator('.status')).to_have_text('定了第 1 条')
        token = self.page.evaluate("() => localStorage.getItem('mvgp-review-csrf')")
        armed = {'on': True}
        def feed(route):
            if armed['on'] and any(p.endswith('/human-decisions') for m, p in self.requests if m == 'POST'):
                armed['on'] = False
                return route.fulfill(status=401, content_type='application/json', body='{"message":"expired"}')
            return route.continue_()
        self.requests.clear()
        self.page.route('**/review-feed', feed)
        self.page.route('**/v1/session/access', lambda r: r.fulfill(status=200, content_type='application/json',
                        body=json.dumps({'member_id': 'm', 'csrf_token': token})))
        card.get_by_role('button', name='撤销选择', exact=True).click()
        expect(card.locator('.status')).to_have_text('待审')
        self.assertEqual([p for m, p in self.requests if m == 'POST' and p.endswith('/human-decisions')],
                         [f'/v1/projects/{self.pid}/human-decisions'])
        with sqlite3.connect(self.store.path) as db:
            choices = [json.loads(b)['choice'] for (b,) in db.execute(
                "SELECT r.body FROM revisions r JOIN objects o USING(project_id, object_id) WHERE o.kind='human-receipt' ORDER BY r.rowid")]
        self.assertEqual(choices, ['confirm', 'decline'])

    def desk_shot(self, label, *, takes=2, clip='desk'):
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie(clip + label, 'pass')], 'video/mp4', 'worker_service')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': label,
            'Direction': {'the goal of the shot in one line': f'{label} 的目标'}}}, 'author')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        made = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service')
                for _ in range(takes)]
        request = f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key=f'offer-{label}', expected_revision=shot['revision'],
            target=ref(shot), shot=ref(shot), takes=[ref(t) for t in made], purpose='take', rationale='拍好了'))
        return shot, made, request, ref

    def open_desk(self):
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()

    def test_a_day_old_offer_is_still_pickable(self):
        """take requests never expire on the desk."""
        shot, takes, request, ref = self.desk_shot('S08-010A')
        self.store.append_revision(self.pid, request['object_id'], request['revision'], {**request['body'], 'expires_at': 0},
                                   'decision_service')
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S08-010A')
        expect(card.locator('.status')).to_have_text('待审')
        card.locator('.take').nth(1).hover()
        with self.page.expect_response(lambda r: r.url.endswith('/human-decisions')) as response:
            card.get_by_role('button', name='用这条', exact=True).click()
        self.assertEqual(response.value.status, 200)
        expect(card.locator('.status')).to_have_text('定了第 2 条')
        self.assertEqual(self.errors, [])

    def test_earlier_batches_and_the_pick_stay_after_the_card_is_patched(self):
        """after 再拍一批 the old batch and the pick stay and stay pickable."""
        shot, takes, request, ref = self.desk_shot('S08-020A')
        from production.contracts import HumanDecision, ObjectRef
        session = owner_session(self.auth)
        human = self.auth.authenticate(session['session_token'], channel='cookie')
        self.fixture.decisions.decide(human, self.pid, HumanDecision(idempotency_key='pre-pick', request_id=request['object_id'],
            target_hash=request['body']['target_hash'], choice='confirm', selected_take=ObjectRef(**ref(takes[0])),
            csrf_token=session['csrf_token']), origin=self.origin)
        patched = self.store.append_revision(self.pid, shot['object_id'], shot['revision'],
                                             {**shot['body'], 'content': {**shot['body']['content'], 'v': 2}}, 'author')
        job = self.store.create_object(self.pid, 'job', {'state': 'running'}, 'worker_service')
        self.store.create_object(self.pid, 'batch', {'state': 'running', 'count': 1,
            'children': [{'target': ref(patched), 'job': ref(job)}]}, 'batch_service')
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S08-020A')
        expect(card.locator('.status')).to_contain_text('生成中')
        batch = card.locator('.batch')
        expect(batch).to_have_count(1)
        expect(batch.locator('.take .tick')).to_have_count(1)
        batch.locator('.take').nth(1).hover()
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as sent:
            batch.get_by_role('button', name='用这条', exact=True).click()
        payload = sent.value.post_data_json
        self.assertEqual((payload['request_id'], payload['selected_take']), (request['object_id'], ref(takes[1])))
        expect(batch.locator('.take').nth(1).locator('.tick')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_earlier_batches_fold_behind_one_line_and_repeats_are_dropped(self):
        """Owner 2026-09-25 "这界面真看不懂": the current takes, then one line for earlier batches; a repeated batch is not shown twice."""
        shot, takes, request, ref = self.desk_shot('S08-040A')
        f = self.fixture
        for n in range(2):  # two more requests offering the very same takes, as the live 9-23 batches did
            self.store.append_revision(self.pid, request['object_id'], self.store.get_object(self.pid, request['object_id'])['revision'],
                                       {**self.store.get_object(self.pid, request['object_id'])['body'], 'state': 'declined'}, 'decision_service')
            request = f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key=f'again-{n}', expected_revision=shot['revision'],
                target=ref(shot), shot=ref(shot), takes=[ref(t) for t in takes], purpose='take', rationale='again'))
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('other-S08-040A', 'pass')], 'video/mp4', 'worker_service')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        other = self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service')
        f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='new-batch', expected_revision=shot['revision'],
            target=ref(shot), shot=ref(shot), takes=[ref(other)], purpose='take', rationale='new'))
        self.open_desk()
        self.page.locator('button[data-filter="all"]').click()
        card = self.page.locator('.card').filter(has_text='S08-040A')
        expect(card.locator('.batch')).to_have_count(0)
        toggle = card.get_by_role('button', name='之前的批次（1）', exact=True)
        expect(toggle).to_have_count(1)
        toggle.click()
        expect(card.locator('.batch')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_the_source_opens_large_with_its_own_segment(self):
        """Owner 2026-09-25 "原片也点不开"."""
        clip = self.recreation_shot('S09-030A')
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S09-030A')
        card.locator('.take.source .open').click()
        expect(self.page.locator('#light')).to_be_visible()
        expect(self.page.locator('#l-ref')).to_have_text('原片 0.5–1.5 秒')
        self.assertTrue(self.page.locator('#l-video').get_attribute('src').endswith('#t=0.5,1.5'))
        expect(self.page.locator('#l-pick')).to_be_disabled()
        self.page.locator('#l-next').click()
        expect(self.page.locator('#l-ref')).to_have_text('第 1 条')
        self.assertEqual(self.errors, [])

    def test_undo_takes_back_a_rebatch(self):
        """(owner: 这个得做啊)."""
        shot, takes, request, ref = self.desk_shot('S08-030A')
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S08-030A')
        card.get_by_role('button', name='再拍一批', exact=True).click()
        # the owner sees what a blank and a written 再拍一批 each do.
        self.assertEqual(card.locator('.note-box input').get_attribute('placeholder'),
                         '不写理由 = 原样再拍 4 条；写了理由 = 交给 AI 改（Enter 记下）')
        card.locator('.note-box input').fill('光太暗')
        with self.page.expect_response(lambda r: r.url.endswith('/human-decisions')):
            card.get_by_role('button', name='记下', exact=True).click()
        expect(self.page.locator('#toast-text')).to_have_text('已记下：S08-030A 再拍一批，交给 AI 改')
        self.page.locator('[data-filter="all"]').click()
        card = self.page.locator('.card').filter(has_text='S08-030A')
        expect(card.locator('.status')).to_have_text('都不行')
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as sent:
            card.get_by_role('button', name='撤销（再拍一批 / 都不行）', exact=True).click()
        self.assertEqual(sent.value.post_data_json['choice'], 'undo')
        expect(card.locator('.status')).to_have_text('待审')
        self.assertEqual(self.errors, [])

    def test_a_blank_rebatch_sends_the_answer_the_automatic_reshoot_reads(self):
        """the desk's blank 再拍一批 is exactly what AutoShoot re-fires by itself."""
        from production.shoot import REASONLESS_REBATCH
        self.desk_shot('S08-035A')
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S08-035A')
        card.get_by_role('button', name='再拍一批', exact=True).click()
        with self.page.expect_request(lambda r: r.url.endswith('/human-decisions')) as sent:
            card.get_by_role('button', name='记下', exact=True).click()
        reason = sent.value.post_data_json['reason']
        self.assertTrue(REASONLESS_REBATCH.match(reason), reason)
        expect(self.page.locator('#toast-text')).to_have_text('已记下：S08-035A 原样再拍 4 条')
        self.assertEqual(self.errors, [])

    def test_notes_are_saved_on_the_platform_on_the_exact_take(self):
        """写一句 reaches the Agent; a reload in a fresh browser still shows it."""
        shot, takes, request, ref = self.desk_shot('S08-040A')
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S08-040A')
        card.locator('.take').nth(1).hover()
        card.get_by_role('button', name='写一句', exact=True).click()
        card.locator('.note-box input').fill('她转头太快')
        with self.page.expect_request(lambda r: r.url.endswith('/owner-notes')) as sent:
            card.get_by_role('button', name='记下', exact=True).click()
        self.assertEqual(sent.value.post_data_json['take'], ref(takes[1]))
        expect(card.locator('.note-text')).to_contain_text('她转头太快')
        note, = self.store.list_objects(self.pid, kind='owner-note')
        self.assertEqual((note['body']['text'], note['body']['take']), ('她转头太快', ref(takes[1])))
        # The note is not kept in this browser any more; only the platform holds it.
        self.assertEqual(self.page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('mvgp-review-notes:'))"), [])
        self.page.reload()
        expect(self.page.locator('.card').filter(has_text='S08-040A').locator('.note-text')).to_contain_text('她转头太快')
        self.assertEqual(self.errors, [])

    def test_notes_left_in_this_browser_are_uploaded_once(self):
        """notes written before this release reach the platform once, at shot level."""
        shot, takes, request, ref = self.desk_shot('S08-060A')
        old = json.dumps([{'id': 1, 'shot': 'S08-060A', 'take': 2, 'at': '1.5', 'text': '眼神再慢一点'}])
        self.page.add_init_script(f"localStorage.setItem('mvgp-review-notes:{self.pid}', {json.dumps(old)})")
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S08-060A')
        expect(card.locator('.note-text')).to_contain_text('以前写的·第2条 @1.5s：眼神再慢一点')
        note, = self.store.list_objects(self.pid, kind='owner-note')
        self.assertIsNone(note['body']['take'])
        self.assertEqual(self.page.evaluate(f"() => localStorage.getItem('mvgp-review-notes:{self.pid}')"), None)
        self.page.reload()
        expect(self.page.locator('.card').filter(has_text='S08-060A').locator('.note-text')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_the_project_page_renews_a_lapsed_session(self):
        """/p/<project> renews through the owner's identity instead of asking to log in."""
        self.app.add_api_route('/p/{pid}', lambda pid: FileResponse(ROOT / 'web/project.html'), methods=['GET'])
        self.open_desk()
        token = self.page.evaluate("() => localStorage.getItem('mvgp-review-csrf')")
        lapsed = {'n': 0}
        def once(route):
            if lapsed['n'] == 0:
                lapsed['n'] += 1
                return route.fulfill(status=401, content_type='application/json', body='{"message":"expired"}')
            return route.continue_()
        self.page.route('**/project-tree', once)
        self.page.route('**/v1/session/access', lambda r: r.fulfill(status=200, content_type='application/json',
                        body=json.dumps({'member_id': 'm', 'csrf_token': token})))
        self.page.goto(self.origin + f'/p/{self.pid}')
        expect(self.page.locator('.hf-folders .tree').get_by_text('全部素材', exact=True)).to_have_count(1)
        expect(self.page.get_by_text('请先在看片台登录')).to_have_count(0)
        self.assertEqual(lapsed['n'], 1)
        self.assertEqual(self.errors, [])

    def test_fal_samples_show_their_1080p_state_and_the_film_plays_the_正片(self):
        """样片 / 正片生成中 / 正片 on each take, the seven-day line, 看成片 plays the 正片."""
        shot, takes, request, ref = self.desk_shot('S10-010A', takes=3)
        full = self.store.create_object(self.pid, 'media', {**takes[0]['body']}, 'worker_service')
        until = int(time.time()) + 3 * 86400
        states = {takes[0]['object_id']: {'state': 'ready', 'take': ref(full), 'expires_at': until},
                  takes[1]['object_id']: {'state': 'making', 'take': None, 'expires_at': until},
                  takes[2]['object_id']: {'state': 'draft', 'take': None, 'expires_at': until}}
        def feed(route):
            body = route.fetch().json()
            body['completions'] = states
            route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
        self.page.route('**/review-feed', feed)
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S10-010A')
        expect(card.locator('.chip.res')).to_have_text(['正片', '正片生成中', '样片'])
        expect(card.locator('.draft-note')).to_contain_text('10 分钟后自动做成 1080p 正片')
        expect(card.locator('.draft-note')).to_contain_text(time.strftime('%Y/%-m/%-d', time.localtime(until)))
        card.locator('.take').nth(0).hover()
        with self.page.expect_response(lambda r: r.url.endswith('/human-decisions')):
            card.get_by_role('button', name='用这条', exact=True).click()
        expect(card.locator('.status')).to_have_text('定了第 1 条')
        states[takes[0]['object_id']] = {'state': 'failed', 'take': None, 'expires_at': until}
        self.page.reload()
        self.page.locator('button[data-filter="all"]').click()  # a picked shot sits under 全部
        card = self.page.locator('.card').filter(has_text='S10-010A')
        expect(card.locator('.status')).to_have_text('定了第 1 条 · 正片没做成，成片先用样片')
        states[takes[0]['object_id']] = {'state': 'ready', 'take': ref(full), 'expires_at': until}
        self.page.reload()
        self.page.locator('#toggle-cut').click()
        segment = self.page.locator('#cut-strip .seg').first
        self.assertIn(f"/media/{full['object_id']}?", segment.locator('video').get_attribute('src'))
        self.assertEqual(self.errors, [])

    def recreation_shot(self, label):
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        project = self.store.get_object(self.pid, self.pid)
        self.store.append_revision(self.pid, self.pid, project['revision'],
                                   {**project['body'], 'branch': 'recreation', 'reader_switch': True}, 'operator')
        clip = f.c.media.put(self.pid, [f.c.synthetic_movie('source-' + label, 'pass')], 'video/mp4', 'author')
        understanding = self.store.create_object(self.pid, 'source-understanding', {'content': {'type': 'source-understanding',
            'source': ref(clip), 'start_seconds': 0.5, 'end_seconds': 1.5}, 'dependencies': [ref(clip)]}, 'author')
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S09', 'dependencies': [ref(understanding)]}, 'author')
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('takes-' + label, 'pass')], 'video/mp4', 'worker_service')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': label}, 'dependencies': [ref(scene)]}, 'author')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service') for _ in range(4)]
        f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key=f'rec-{label}', expected_revision=shot['revision'],
            target=ref(shot), shot=ref(shot), takes=[ref(t) for t in takes], purpose='take', rationale='拍好了'))
        return clip

    def test_recreation_card_plays_the_source_segment_next_to_the_takes(self):
        """(owner: 复刻时看片台上看不到原片那一段…这个最好还是提供吧)."""
        clip = self.recreation_shot('S09-010A')
        self.open_desk()
        card = self.page.locator('.card').filter(has_text='S09-010A')
        expect(card.locator('.takes.src .take')).to_have_count(5)
        source = card.locator('.take.source')
        expect(source.locator('.chip.n')).to_have_text('原片')
        self.assertTrue(source.locator('video').get_attribute('src').endswith(f"/media/{clip['object_id']}?revision=1#t=0.5,1.5"))
        expect(source.get_by_role('button', name='用这条', exact=True)).to_have_count(0)
        source.hover()
        self.page.wait_for_function("() => { const v = document.querySelector('.take.source video'); return v && v.currentTime >= 0.5; }")
        self.assertEqual(self.errors, [])

    def test_recreation_settings_have_the_source_reading_switch(self):
        """on by default for a new recreation project; the owner can turn it off."""
        self.recreation_shot('S09-020A')
        self.open_desk()
        self.page.locator('#open-settings').click()
        switch = self.page.locator('#steps input[name="source_reading"]')
        expect(switch).to_be_checked()
        with self.page.expect_request(lambda r: r.url.endswith('/switches') and r.method == 'POST') as sent:
            switch.uncheck()
        self.assertEqual(sent.value.post_data_json['switches'], {'source_reading': False})
        expect(switch).not_to_be_checked()
        self.assertEqual(self.errors, [])

    def test_project_page_shot_folder_shows_the_source_segment_first(self):
        """A recreation shot folder shows the source segment before its takes."""
        clip = self.recreation_shot('S09-030A')
        self.open_desk()
        self.page.locator('#proj-page').click()
        view = self.page.locator('#project-view')
        view.locator('.hf-folders .tree').get_by_text('S09', exact=True).click()
        view.locator('.hf-folders .tree').get_by_text('S09-030A', exact=True).click()
        tiles = view.locator('.hf-grid .hf-tile')
        expect(tiles).to_have_count(5)
        first = view.locator('.hf-tile', has=self.page.locator('.badge', has_text='原片'))
        expect(first).to_have_count(1)
        self.assertTrue(first.locator('video').get_attribute('src').endswith('#t=0.5,1.5'))
        first.click()
        expect(view.locator('.hfx__details')).to_contain_text('原片区间')
        expect(view.locator('.hfx__details')).to_contain_text('0.5–1.5 秒')
        self.assertEqual(self.errors, [])

    def test_project_page_shows_brief_stage_shotlist_and_version_log(self):
        """HF's project records on the project page; the desk lists each project's stage."""
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S10 · 门口'}, 'author')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S10-010A', 'Camera': {'shot size': 'MCU', 'lens': '50mm'},
            'The material': {'the running time in seconds': 6}, 'Direction': {'the goal of the shot in one line': '她在门口停住'}},
            'dependencies': [ref(scene)]}, 'author')
        raw = self.fixture.c.media.put(self.pid, [self.fixture.c.synthetic_movie('log', 'pass')], 'video/mp4', 'worker_service')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'task': 'shot', 'dependencies': [ref(shot)],
            'compilation': {'job_type': 'fal_seedance_2_5', 'authorship': {'change_note': '第一版'}}}, 'compiler_service')
        self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service')
        # the order carries the reviewer's notes and the writer's answers.
        self.store.create_object(self.pid, 'shoot-order', {'request': {'idempotency_key': 'o'}, 'cards': [
            {'card': ref(shot), 'takes': 4, 'stage': 'on-desk', 'shot': 'S10-010A'}], 'review': {'reviewer': 'fresh reviewer', 'notes': [
            {'line': 'cinedance:336', 'note': '首帧是空的', 'answer': 'FIRST FRAME AND SPATIAL BLOCKING', 'shot': 'S10-010A'},
            {'line': 'cinedance:1176', 'note': '另一颗镜头的意见', 'answer': 'POSITIVE CONSTRAINTS', 'shot': 'S10-020A'}]},
            'dependencies': [ref(shot)]}, 'shoot_service')
        self.open_desk()
        expect(self.page.locator('.project .stage')).to_have_text('拍摄')
        self.page.locator('#proj-page').click()
        view = self.page.locator('#project-view')
        expect(view.locator('.hf-stage .on')).to_have_text('拍摄')
        expect(view.locator('.hf-brief')).to_contain_text('fal_seedance_2_5')
        tree = view.locator('.hf-folders .tree')
        tree.get_by_text('S10 · 门口', exact=True).click()
        expect(view.locator('.hf-shotlist tr')).to_have_count(2)
        expect(view.locator('.hf-shotlist')).to_contain_text('她在门口停住')
        tree.get_by_text('S10-010A', exact=True).click()
        expect(view.locator('.hf-goal')).to_have_text('她在门口停住')
        expect(view.locator('.hf-log .row')).to_have_count(1)
        expect(view.locator('.hf-log')).to_contain_text('第一版')
        review = view.locator('.hf-review')
        expect(review.locator('.row')).to_have_count(1)  # only this shot's note
        expect(review.locator('.row')).to_contain_text('cinedance:336')
        expect(review.locator('.row')).to_contain_text('改了：FIRST FRAME AND SPATIAL BLOCKING')
        expect(review.locator('.chip')).to_have_text(['手册不是最新'])  # the candidate names no current manuals
        # A card that leaves the route out is priced on the route the compiler fills in.
        expect(review.locator('.quote')).to_contain_text('拍一批 4 条约 4 synthetic_unit')
        self.assertEqual(self.errors, [])

    def test_an_ordered_shot_without_a_review_shows_没审(self):
        """(Q6): the reviewer step is recorded and shown, never a gate."""
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        from production import playbook
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S11 · 路口'}, 'author')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S11-010A', 'The material': {'the running time in seconds': 5},
            'Direction': {'the goal of the shot in one line': '车停在路口'},
            '_production': {'model': 'seedance_2_5', 'resolution': '1080p', 'aspect_ratio': '16:9'}}, 'dependencies': [ref(scene)]}, 'author')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'task': 'shot', 'dependencies': [ref(shot)],
            'compilation': {'job_type': 'seedance_2_5', 'authorship': {'playbook_version': playbook.version()}}}, 'compiler_service')
        raw = self.fixture.c.media.put(self.pid, [self.fixture.c.synthetic_movie('s11', 'pass')], 'video/mp4', 'worker_service')
        self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service')
        self.store.create_object(self.pid, 'shoot-order', {'request': {'idempotency_key': 'o2'}, 'cards': [
            {'card': ref(shot), 'takes': 4, 'stage': 'on-desk', 'shot': 'S11-010A'}], 'dependencies': [ref(shot)]}, 'shoot_service')
        self.open_desk()
        self.page.locator('#proj-page').click()
        view = self.page.locator('#project-view')
        tree = view.locator('.hf-folders .tree')
        tree.get_by_text('S11 · 路口', exact=True).click()
        tree.get_by_text('S11-010A', exact=True).click()
        review = view.locator('.hf-review')
        expect(review.locator('.chip')).to_have_text(['没审'])
        expect(review.locator('.row')).to_have_count(0)
        expect(review.locator('.quote')).to_contain_text('拍一批 4 条约')
        self.assertEqual(self.errors, [])

    def test_a_failed_video_renews_the_session_and_retries_once(self):
        """an idle desk's cookie lapses; the video renews and reloads instead of going blank."""
        shot, takes, request, ref = self.desk_shot('S08-050A', takes=1)
        self.open_desk()
        token = self.page.evaluate("() => localStorage.getItem('mvgp-review-csrf')")
        media = f"/v1/projects/{self.pid}/media/{takes[0]['object_id']}"
        refused = {'n': 0}
        def once(route):
            if refused['n'] == 0:
                refused['n'] += 1
                return route.fulfill(status=401, content_type='application/json', body='{"message":"expired"}')
            return route.continue_()
        self.page.route('**' + media + '*', once)
        self.page.route('**/v1/session/access', lambda r: r.fulfill(status=200, content_type='application/json',
                        body=json.dumps({'member_id': 'm', 'csrf_token': token})))
        self.requests.clear()
        self.page.locator('[data-filter="all"]').click()
        self.page.wait_for_function('() => [...document.querySelectorAll(".card video")].some(v => v.readyState >= 1)')
        self.assertIn(('POST', '/v1/session/access'), self.requests)
        self.assertGreaterEqual(sum(1 for m, p in self.requests if m == 'GET' and p.startswith(media)), 2)
        self.assertEqual(self.errors, [])

    def test_the_project_view_scrolls_and_logging_out_closes_it(self):
        """Bug hunt 2026-09-24: the embedded project page scrolls; 退出 does not leave it showing."""
        for n in range(30):
            self.store.create_object(self.pid, 'asset', {'content': {'type': 'asset', 'role': 'visual', 'tag': f'@p{n:02d}',
                'category': 'prop', 'definition': {'descriptor': 'x'}}}, 'author')
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        self.page.locator('#proj-page').click()
        view = self.page.locator('#project-view')
        expect(view.locator('.hf-tile')).to_have_count(30)
        self.assertEqual(view.evaluate('e => getComputedStyle(e).overflowY'), 'auto')
        self.assertGreater(view.evaluate('e => e.scrollHeight'), view.evaluate('e => e.clientHeight'))
        self.assertLess(self.page.locator('.hf-tile .hf-fail').first.bounding_box()['height'], 200)
        self.page.locator('#open-settings').click()
        self.page.locator('#logout').click()
        expect(self.page.locator('#login')).to_be_visible()
        expect(view).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_a_pick_from_an_earlier_batch_plays_in_the_film_strip_while_a_new_batch_waits(self):
        # dry run: the strip showed the shot as 未定 although the film used the owner's earlier pick.
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('earlier', 'pass')], 'video/mp4', 'worker_service')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S07-010A'}}, 'author')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service') for _ in range(4)]
        f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='round-1', expected_revision=1, target=ref(shot),
            shot=ref(shot), takes=[ref(t) for t in takes[:2]], purpose='take', rationale='r'))
        self.store.create_object(self.pid, 'human-take-selection', {'shot': ref(shot), 'take': ref(takes[0]),
            'verified_human_session': True, 'dependencies': [ref(shot), ref(takes[0])]}, 'decision_service')
        f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='round-2', expected_revision=1, target=ref(shot),
            shot=ref(shot), takes=[ref(t) for t in takes[2:]], purpose='take', rationale='r'))
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('.card').filter(has_text='S07-010A')).to_be_visible()
        self.page.locator('#toggle-cut').click()
        segment = self.page.locator('#cut-strip .seg').filter(has_text='S07-010A')
        expect(segment).to_have_count(1)
        expect(segment).not_to_have_class(re.compile(r'missing'))
        expect(segment).not_to_contain_text('未定')
        self.assertEqual(self.errors, [])

    def test_returning_from_the_project_page_shows_the_cards_again(self):
        """Release 81 rehearsal walk: after 项目页 → 回到看片 the pending cards must show again."""
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('back', 'pass')], 'video/mp4', 'worker_service')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S09-010A'}}, 'author')
        cand = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        takes = [self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(cand)]}, 'worker_service') for _ in range(2)]
        f.decisions.request(f.c.actor, self.pid, DecisionRequest(idempotency_key='back', expected_revision=1, target=ref(shot),
            shot=ref(shot), takes=[ref(t) for t in takes], purpose='take', rationale='r'))
        self.page.set_viewport_size({'width': 1440, 'height': 900})
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        card = self.page.locator('.card').filter(has_text='S09-010A')
        expect(card).to_be_visible()
        self.page.locator('#proj-page').click()
        view = self.page.locator('#project-view')
        expect(view).to_be_visible()
        view.locator('.hf-tile').first.click()
        expect(view.locator('.hfx')).to_be_visible()
        self.page.keyboard.press('Escape')
        expect(view.locator('.hfx')).to_be_hidden()
        self.page.locator('#proj-page').click()
        expect(card).to_be_visible()
        self.assertGreater(card.bounding_box()['height'], 100)
        self.assertEqual(self.errors, [])

    def test_personal_settings_has_the_ledger_with_the_providers_own_record(self):
        """个人设置 → 账本: every paid call, and 平台记的 vs fal 自己的账 with the difference marked."""
        import datetime as dt
        import json as js
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        self.store.set_budget(self.pid, 50_000_000, 'usd_micro', budget_key='fal_owner')
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S04-ROAD\n'}, 'author')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S04-010A'}, 'dependencies': [ref(scene)]}, 'author')
        candidate = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        intent = self.store.create_object(self.pid, 'dispatch-intent', {'operation': 'submit', 'candidate': ref(candidate),
            'request': {'job_type': 'fal_seedance_2_5'}, 'cost': {'budget_key': 'fal_owner'}}, 'submission_service')
        self.store.reserve(self.pid, 'led-1', 6_232_230, 'usd_micro', object_id=intent['object_id'], budget_key='fal_owner')
        self.store.settle(self.pid, 'led-1', 830_962)
        self.store.create_object(self.pid, 'job', {'intent': ref(intent), 'state': 'succeeded', 'reservation_id': 'led-1',
                                 'remote_job_id': '01a0d7fa-efe3-79b2-b20f-62cc6d49e740'}, 'submission_service')
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        folder = self.store.path.parent / 'ledger'
        folder.mkdir(exist_ok=True)
        (folder / f'fal-{day}.json').write_text(js.dumps({'complete': False, 'results': [
            {'endpoint_id': 'bytedance/seedance-2.5/reference-to-video', 'cost_total': 1.661924, 'currency': 'USD'}]}))
        # a Higgsfield take matched to its own spend; other account activity listed apart.
        self.store.set_budget(self.pid, 8000, 'hf_credit', budget_key='hf_owner')
        hf_intent = self.store.create_object(self.pid, 'dispatch-intent', {'operation': 'submit', 'candidate': ref(candidate),
            'request': {'job_type': 'seedance_2_5'}, 'cost': {'budget_key': 'hf_owner'}}, 'submission_service')
        self.store.reserve(self.pid, 'led-hf', 360, 'hf_credit', object_id=hf_intent['object_id'], budget_key='hf_owner')
        self.store.settle(self.pid, 'led-hf', 48)
        self.store.create_object(self.pid, 'job', {'intent': ref(hf_intent), 'state': 'succeeded', 'reservation_id': 'led-hf',
                                 'remote_job_id': '0f59c1fd-300f-4ecd-a6e4-c2323d966c98'}, 'submission_service')
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=20)).isoformat().replace('+00:00', 'Z')
        (folder / f'higgsfield-{day}.json').write_text(js.dumps({'complete': False, 'balance': {'credits': 8000.00}, 'transactions': [
            {'action': 'spend', 'created_at': soon, 'credits': -48, 'display_name': 'Seedance 2.5'},
            {'action': 'spend', 'created_at': soon, 'credits': -48, 'display_name': 'Wan 3.0 Prime Video'}]}))
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        self.page.locator('#open-settings').click()
        expect(self.page.locator('#settings h2')).to_have_text('个人设置')
        self.page.get_by_role('button', name='账本', exact=True).click()
        panel = self.page.locator('#ledger')
        expect(panel).to_be_visible()
        expect(panel.locator('.ledger-row')).to_have_count(2)
        expect(panel.locator('.ledger-row').first).to_contain_text('样片 · fal · 请求 01a0d7fa-efe3-79b2-b20f-62cc6d49e740')
        hf = panel.locator('.ledger-day', has_text=f'{day} · Higgsfield')
        expect(hf).to_contain_text('平台记的 48 点（对上 1 条）· Higgsfield 账上扣了 96 点 · 其中不是平台花的 48 点（Wan 3.0 Prime Video 48 点） · 差 0 点')
        expect(hf).to_contain_text('余额 8000 点')
        expect(panel.locator('.ledger-row').first).to_contain_text('已结 $0.83')
        expect(panel.locator('.ledger-group')).to_contain_text('S04-010A')
        diff = panel.locator('.ledger-day.diff')
        expect(diff).to_have_count(1)
        expect(diff).to_contain_text('平台记的 $0.83 · fal 自己的 $1.66 · 差 $0.83')
        panel.locator('select[name="view"]').select_option('按供应商')
        expect(panel.locator('.ledger-group')).to_have_text(['fal · $0.83', 'higgsfield · 48 点'])
        self.assertEqual(self.errors, [])

    def test_settings_generation_record_lists_what_where_when_and_cost(self):
        """Settings → 生成记录 in the desk the owner uses."""
        f = self.fixture
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        raw = f.c.media.put(self.pid, [f.c.synthetic_movie('record', 'pass')], 'video/mp4', 'worker_service')
        self.store.set_budget(self.pid, 1000, 'hf_credit', budget_key='hf_owner')
        scene = self.store.create_object(self.pid, 'scene', {'content': '# S03-DOOR\n'}, 'author')
        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S03-010A'}, 'dependencies': [ref(scene)]}, 'author')
        candidate = self.store.create_object(self.pid, 'candidate', {'target': ref(shot), 'dependencies': [ref(shot)]}, 'compiler_service')
        for n, state in enumerate(('succeeded', 'failed')):
            intent = self.store.create_object(self.pid, 'dispatch-intent', {'operation': 'submit', 'target': ref(shot),
                'candidate': ref(candidate), 'request': {'job_type': 'seedance_2_5'}}, 'submission_service')
            take = self.store.create_object(self.pid, 'media', {**raw['body'], 'dependencies': [ref(candidate)]}, 'worker_service') if state == 'succeeded' else None
            self.store.create_object(self.pid, 'job', {'intent': ref(intent), 'state': state, **({'result': ref(take)} if take else {})},
                                     'submission_service')
            self.store.reserve(self.pid, f'rec-{n}', 60, 'hf_credit', object_id=intent['object_id'], budget_key='hf_owner')
            self.store.settle(self.pid, f'rec-{n}', 56 if state == 'succeeded' else None)
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        self.page.locator('#open-settings').click()
        self.page.get_by_role('button', name='生成记录', exact=True).click()
        panel = self.page.locator('#record')
        expect(panel).to_be_visible()
        rows = panel.locator('.record-row')
        expect(rows).to_have_count(2)
        # Newest first, like the HF generation list.
        expect(rows.nth(1)).to_contain_text('片子')
        expect(rows.nth(1)).to_contain_text('seedance_2_5')
        expect(rows.nth(1)).to_contain_text('S03-DOOR · S03-010A · 第 1 条')
        expect(rows.nth(1)).to_contain_text('成功')
        expect(rows.nth(1)).to_contain_text('56 点')
        expect(rows.nth(1).locator('video')).to_have_count(1)
        expect(rows.nth(0)).to_contain_text('失败')
        expect(rows.nth(0)).to_contain_text('最多 60 点（未结算）')
        expect(panel.locator('.record-totals')).to_contain_text('Higgsfield 点数：已结算 56 点 · 未结算最多 60 点 · 共 2 笔')
        panel.locator('select[name="status"]').select_option('失败')
        expect(rows).to_have_count(1)
        panel.get_by_role('button', name='关闭', exact=True).click()
        expect(panel).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_desk_shows_shoot_orders_firing_and_stopped(self):
        """A shot the platform is still shooting says where it is (no plan or listening stage any more)."""
        def ref(obj):
            return {key: obj[key] for key in ('object_id', 'revision', 'digest')}
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        cards = []
        for label, stage, reason in (('S05-010A', 'firing', None),
                                     ('S05-030A', 'stopped', 'Every take failed at Higgsfield; rewrite one line and order again')):
            shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': label,
                'The material': {'the action in one to three sentences': f'{label} action.'}}}, 'author')
            cards.append({'card': ref(shot), 'shot': label, 'stage': stage, 'reason': reason, 'takes': 4,
                          **({'code': 'takes_failed'} if stage == 'stopped' else {})})
        self.store.create_object(self.pid, 'shoot-order', {'request': {}, 'cards': [{**cards[0], 'stage': 'stopped',
            'reason': 'An older order that was ordered again'}], 'dependencies': [cards[0]['card']]}, 'shoot_service')
        self.store.create_object(self.pid, 'shoot-order', {'request': {}, 'cards': cards,
            'dependencies': [c['card'] for c in cards]}, 'shoot_service')  # the latest order per card wins
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        desk = self.page.locator('.card')
        expect(desk.filter(has_text='S05-010A').locator('.status')).to_have_text('收片中')
        # the owner reads a plain-Chinese reason; the English one stays for the agent.
        expect(desk.filter(has_text='S05-030A').locator('.status')).to_contain_text('停下了：这一批一条都没出来')
        self.assertEqual(self.errors, [])

    def test_settings_has_only_the_stress_test_switch_and_it_turns_on_and_off(self):
        """the old 拍片步骤 stay dormant; one switch, off by default."""
        from production import switches
        self.app.add_api_route('/review', lambda: FileResponse(ROOT / 'web/review.html'), methods=['GET'])
        self.owner_signed_in()
        self.page.goto(self.origin + '/review')
        expect(self.page.locator('#login')).to_be_hidden()
        self.page.locator('#open-settings').click()
        steps = self.page.locator('#steps')
        # 样片模式 is the second switch (fal drafts instead of Higgsfield), off by default.
        expect(steps.locator('input[type="checkbox"]')).to_have_count(2)
        expect(steps).to_contain_text('素材先做压力测试')
        expect(steps).to_contain_text('样片模式')
        expect(steps).not_to_contain_text('AI 导演')
        sample = steps.locator('input[name="sample_mode"]')
        expect(sample).not_to_be_checked()
        sample.click()
        expect(self.page.locator('#toast-text')).to_contain_text('样片模式：已打开')
        self.assertTrue(switches.current(self.store, self.fixture.c.flow.config, self.pid)['sample_mode'])
        sample.click()
        expect(self.page.locator('#toast-text')).to_contain_text('样片模式：已关闭')
        self.assertFalse(switches.current(self.store, self.fixture.c.flow.config, self.pid)['sample_mode'])
        box = steps.locator('input[type="checkbox"]').first
        expect(box).not_to_be_checked()
        box.click()
        expect(self.page.locator('#toast-text')).to_contain_text('素材压力测试：已打开')
        self.assertTrue(switches.current(self.store, self.fixture.c.flow.config, self.pid)['asset_stress_test'])
        box.click()
        expect(self.page.locator('#toast-text')).to_contain_text('素材压力测试：已关闭')
        self.assertFalse(switches.current(self.store, self.fixture.c.flow.config, self.pid)['asset_stress_test'])
        # Bug hunt r82 P1: a failed save puts the box back to what the server holds.
        self.page.route('**/switches', lambda route: route.fulfill(status=503, content_type='application/json',
            body='{"code":"unavailable","message":"x"}') if route.request.method == 'POST' else route.continue_())
        box.click()
        expect(box).not_to_be_checked()
        self.assertFalse(switches.current(self.store, self.fixture.c.flow.config, self.pid)['asset_stress_test'])

    def test_delayed_selected_image_reserves_space_without_late_scroll(self):
        import io

        from PIL import Image

        stream = io.BytesIO()
        Image.new('RGB', (2048, 1360), '#586a71').save(stream, format='PNG')
        images = [self.fixture.c.media.put(self.pid, [stream.getvalue()], 'image/png', 'upload_service')
                  for _ in range(12)]
        selected = images[-1]
        delayed = []
        def hold(route):
            delayed.append(route)
        self.page.route(f'**/media/{selected["object_id"]}?revision=1', hold)
        self.addCleanup(lambda: self.page.unroute_all(behavior='ignoreErrors'))
        self.login(path=self.object_url(selected))
        for width, height in ((1200, 900), (390, 844)):
            self.page.set_viewport_size({'width': width, 'height': height})
            if width == 390:
                self.page.reload()
            expect(self.page.locator('#artifact-view')).to_be_visible()
            until = time.monotonic() + 5
            while not delayed and time.monotonic() < until:
                self.page.wait_for_timeout(50)
            self.assertTrue(delayed)
            before = self.page.locator('#image').bounding_box()
            self.assertGreater(before['height'], 150)
            self.assertGreaterEqual(before['y'], 0)
            self.assertLessEqual(before['y'] + before['height'], height, f'Viewport {width}x{height}')
            # The user may scroll while bytes arrive; loading must not hijack it.
            self.page.mouse.wheel(0, -150)
            self.page.wait_for_timeout(150)
            scroll = self.page.evaluate('scrollY')
            route = delayed.pop(0)
            route.fulfill(response=route.fetch())
            self.page.wait_for_function('document.getElementById("image").naturalWidth === 2048')
            after = self.page.locator('#image').bounding_box()
            self.assertAlmostEqual(after['height'], before['height'], delta=1)
            self.assertAlmostEqual(self.page.evaluate('scrollY'), scroll, delta=1)
            self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.assertEqual(self.errors, [])

    def test_asset_reference_thumbnail_prompt_and_no_production_controls(self):
        f = self.fixture
        image = f.c.media.put(self.pid, [self.png()], 'image/png', 'upload_service')
        ref = {k: image[k] for k in ('object_id', 'revision', 'digest')}
        candidate = self.store.create_object(self.pid, 'candidate', {'request': {'job_type': 'test',
            'params': {'prompt': 'Keep the rider ahead of the wagon.'}, 'references': [{'object_ref': ref, 'media_type': 'image/png'}]},
            'dependencies': []}, 'compiler_service')
        self.login(path=self.object_url(candidate))
        expect(self.page.locator('#artifact-content')).to_contain_text('Keep the rider ahead of the wagon.')
        thumbnail = self.page.locator('#artifact-content img')
        expect(thumbnail).to_have_count(1)
        self.page.wait_for_function('() => document.querySelector("#artifact-content img").naturalWidth > 0')
        self.assertFalse(self.page.locator('input:not([type=password]),[contenteditable]').count())
        self.assertEqual({url for method, url in self.requests if method == 'POST'}, {'/v1/session/exchange'})
        self.assertEqual(self.errors, [])

    @staticmethod
    def png():
        import io

        from PIL import Image
        out = io.BytesIO()
        Image.new('RGB', (24, 16), 'green').save(out, format='PNG')
        return out.getvalue()


if __name__ == '__main__':
    unittest.main()
