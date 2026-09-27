"""Desktop/narrow acceptance against a real TLS API and real served media.

Final labels use explicit trusted display fixtures, not claims of paid generation
or artistic approval. Console/screenshots go to MVGP_BROWSER_ARTIFACTS (or /tmp).
No credentials, request bodies or browser storage are written as test evidence.
"""
import json
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect

from production.auth import SYSTEM_PROJECT
from production.tests import test_web_app


def ref(obj):
    return {key: obj[key] for key in ('object_id', 'revision', 'digest')}


class BrowserAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.h = test_web_app.WebAppTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.page, self.f = self.h.page, self.h.fixture
        self.pid, self.store = self.h.pid, self.h.store
        self.artifacts = Path(os.environ.get('MVGP_BROWSER_ARTIFACTS', '/tmp/mvgp-browser-acceptance'))
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.console = []
        self.page.on('console', lambda message: self.console.append({'type': message.type, 'text': message.text}))
        self.width_name = 'desktop'

    def tearDown(self):
        evidence = {'test': self._testMethodName, 'page_errors': self.h.errors,
                    'console': self.console, 'requests': self.h.requests,
                    'paid_provider_calls': len(self.f.c.provider_calls), 'paid_review_calls': len(self.f.c.review_calls)}
        (self.artifacts / (self._testMethodName + '.json')).write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        self.assertEqual(self.h.errors, [])
        self.assertEqual(self.f.c.provider_calls, [])
        self.assertEqual(self.f.c.review_calls, [])

    def viewport(self, name):
        self.width_name = name
        self.page.set_viewport_size({'width': 1280, 'height': 900} if name == 'desktop' else {'width': 390, 'height': 844})

    def screenshot(self, name):
        self.page.screenshot(path=str(self.artifacts / f'{self.width_name}-{name}.png'), full_page=True)
        overflow = self.page.evaluate('() => document.documentElement.scrollWidth > window.innerWidth + 1')
        self.assertFalse(overflow, f'Horizontal overflow in {self.width_name}/{name}')

    def no_production_controls(self):
        self.assertEqual(self.page.locator('form').count(), 1)
        self.assertEqual(self.page.locator('input:not([type=password]),[contenteditable=true]').count(), 0)
        self.assertTrue(self.page.locator('textarea').evaluate_all('(nodes)=>nodes.every(n=>n.readOnly)'))
        for word in ('生成视频', '重新生成', '编辑提示词', '切换模型', '提交生成', '上传素材'):
            self.assertEqual(self.page.get_by_role('button', name=word, exact=True).count(), 0)
        self.assertTrue(all(method == 'GET' or url == '/v1/session/exchange' or url.endswith('/reference')
                            for method, url in self.h.requests))

    def node(self, kind, body, author='author'):
        return self.store.create_object(self.pid, kind, body, author)

    def movie(self, name, imported=False):
        raw = self.f.c.synthetic_movie(name, 'pass')
        media = self.f.c.media.put(self.pid, [raw], 'video/mp4', 'importer_service' if imported else 'worker_service',
                                   logical_path=f'Films/{name}.mp4')
        if imported:
            media = self.store.append_revision(self.pid, media['object_id'], 1,
                {**media['body'], 'import_status': 'imported-unverified', 'label': 'final'}, 'importer_service')
        return media

    def test_empty_loading_and_revoked_session_at_both_widths(self):
        self.h.login(path=f'/projects/{self.pid}')
        for width in ('desktop', 'narrow'):
            with self.subTest(width=width):
                self.viewport(width)
                expect(self.page.locator('#results-empty')).to_be_visible()
                self.screenshot('empty-project')
                wait = threading.Event()
                original = self.f.services.queries.project
                def delayed(*args, wait=wait, original=original, **kwargs):
                    if not wait.wait(10):
                        raise RuntimeError('Test-only loading gate timed out')
                    return original(*args, **kwargs)
                with patch.object(self.f.services.queries, 'project', side_effect=delayed):
                    try:
                        self.page.locator('#refresh').click()
                        expect(self.page.locator('#notice')).to_have_text('读取项目中…')
                        self.screenshot('loading')
                    finally:
                        wait.set()
                    expect(self.page.locator('#project-view')).to_be_visible()
                self.no_production_controls()
        # Revocation is checked on real API requests, not simulated by hiding DOM.
        credential = next(o for o in self.store.list_objects(SYSTEM_PROJECT, kind='credential')
                          if o['body'].get('actor_id') == 'screening_viewer')
        self.h.auth.revoke(credential['object_id'])
        for width in ('desktop', 'narrow'):
            self.viewport(width)
            self.page.reload()
            expect(self.page.locator('#login')).to_be_visible()
            expect(self.page.locator('#project-view')).to_be_hidden()
            self.assertEqual(self.page.request.get(self.h.origin + '/v1/session').status, 401)
            self.screenshot('unauthorized')
        self.h.browser_context.clear_cookies()
        secret = self.h.auth.provision_token('empty_viewer', 'viewer', [], 300)
        self.page.goto(self.h.origin + '/projects')
        expect(self.page.locator('#login')).to_be_visible()
        self.page.get_by_text('管理员提供的访问凭证', exact=True).click()
        self.page.locator('#session-secret').fill(secret)
        self.page.locator('#login-form button').click()
        for width in ('desktop', 'narrow'):
            self.viewport(width)
            expect(self.page.locator('#projects-empty')).to_be_visible()
            expect(self.page.locator('.project-card')).to_have_count(0)
            self.screenshot('empty-project-list')

    def test_statuses_are_distinct_and_final_requires_trusted_evidence(self):
        scene = self.f.c.draft('scene', 'EP01/scene.md', '篷车前行，骑手从后方逼近。')
        # A shot is 旧版 when something it was written from changed; its scene does not count.
        script = self.f.c.draft('script', 'EP01/script.md', '篷车前行。')
        shot = self.f.c.draft('shot', 'EP01/S01/card.json', {'动作': '骑手快速超过马车。'}, [scene, script])
        self.store.append_revision(self.pid, script['object_id'], 1, {**script['body'], 'content': '修订后的剧本。'}, 'author')
        self.node('job', {'state': 'failed', 'logical_path': 'Results/失败任务', 'dependencies': []}, 'worker_service')
        imported = self.movie('imported', True)
        forged = self.node('final', {'accepted': True, 'media': ref(imported), 'dependencies': [], 'logical_path': 'Results/自称完成'})
        finished = self.movie('reviewed')
        # Synthetic service-owned records verify the display boundary only.
        human = self.node('human-receipt', {'target': ref(finished), 'purpose': 'final', 'choice': 'confirm',
                          'verified_human_session': True, 'dependencies': []}, 'decision_service')
        review = self.node('review-receipt', {'target': ref(finished), 'purpose': 'cut', 'role': 'director',
                           'verdict': 'pass', 'issues': [], 'dependencies': []}, 'review_service')
        final = self.node('final', {'accepted': True, 'media': ref(finished), 'human_receipt': ref(human),
            'review_receipts': [ref(review)], 'dependencies': [ref(finished), ref(human), ref(review)],
            'logical_path': 'Results/已确认影片'}, 'decision_service')
        self.h.login()
        expect(self.page.locator('.project-card')).to_have_count(1)
        for width in ('desktop', 'narrow'):
            with self.subTest(width=width):
                self.viewport(width)
                self.page.goto(self.h.origin + '/projects')
                expect(self.page.locator('.project-card')).to_contain_text('已确认')
                self.screenshot('project-list')
                self.page.locator('.project-card').click()
                expect(self.page.locator('#blockers')).to_contain_text('失败')
                rows = self.page.locator('#result-list .artifact-row')
                expect(rows.filter(has_text='失败任务')).to_contain_text('失败')
                expect(rows.filter(has_text='imported.mp4')).to_contain_text('尚未验证')
                expect(rows.filter(has_text='自称完成')).not_to_contain_text('已确认成片')
                expect(rows.filter(has_text='已确认影片')).to_contain_text('已确认成片')
                self.screenshot('result-statuses')
                self.page.get_by_role('button', name='计划', exact=True).click()
                self.page.locator('#plan-list .old-results summary').click()
                expect(self.page.locator('#plan-list .artifact-row').filter(has_text='card.json')).to_contain_text('旧版')
                self.screenshot('stale-shot')
                for obj, label in ((shot, '旧版'), (forged, '草稿'), (final, '已确认成片')):
                    self.page.goto(self.h.origin + self.h.object_url(obj))
                    expect(self.page.locator('#artifact-status')).to_contain_text(label)
                self.screenshot('confirmed-record')
                self.no_production_controls()

    def test_media_seek_history_refresh_copy_and_lazy_tree_at_both_widths(self):
        media = self.movie('chase')
        previous = ref(media)
        media = self.store.append_revision(self.pid, media['object_id'], 1,
            {**media['body'], 'label': 'Second revision'}, 'worker_service')
        self.h.login(path=self.h.object_url(media, 2))
        self.page.evaluate('Object.defineProperty(navigator, "clipboard", {configurable:true,value:{writeText:async()=>{throw Error("denied")}}})')
        for width in ('desktop', 'narrow'):
            with self.subTest(width=width):
                self.viewport(width)
                self.page.locator('#versions').select_option('1')
                expect(self.page.locator('#artifact-status')).to_contain_text('旧版')
                self.page.wait_for_function('() => document.getElementById("video").readyState >= 1')
                self.page.locator('#video').evaluate('(video)=>video.play()')
                self.page.wait_for_function('() => document.getElementById("video").currentTime > 0.1')
                self.page.locator('#video').evaluate('(video)=>{video.pause();video.currentTime=1.25}')
                expect(self.page.locator('#playback-time')).to_contain_text('1.25')
                self.page.wait_for_function('() => {const v=document.getElementById("video");return !v.seeking && v.readyState>=2}')
                with self.page.expect_request(lambda r: r.url.endswith('/reference')) as request:
                    self.page.locator('#copy-reference').click()
                self.assertEqual(request.value.post_data_json['target'], previous)
                self.assertAlmostEqual(request.value.post_data_json['seconds'], 1.25, places=2)
                expect(self.page.locator('#copy-fallback')).to_be_visible()
                copied = self.page.locator('#copy-text').input_value()
                self.assertIn('revision=1&seconds=1.25', copied)
                self.assertIn(previous['digest'], copied)
                self.screenshot('video-reference')
                self.page.goto(self.h.origin + self.h.object_url(media) + '&seconds=1.25')
                self.page.wait_for_function('() => document.getElementById("video").currentTime >= 1.24')
                self.page.reload()
                self.page.wait_for_function('() => document.getElementById("video").currentTime >= 1.24')
                expect(self.page.locator('#versions')).to_have_value('1')
                self.page.get_by_role('button', name='文件', exact=True).click()
                directory = self.page.locator('#file-tree button').filter(has_text='Films')
                expect(directory).to_be_visible()
                directory.click()
                expect(self.page.locator('#file-tree button').filter(has_text='chase.mp4')).to_be_visible()
                self.screenshot('file-tree')
                self.page.locator('#file-tree button').filter(has_text='chase.mp4').click()
                expect(self.page.locator('#versions')).to_have_value('2')
                self.page.evaluate('Object.defineProperty(navigator, "clipboard", {configurable:true,value:{writeText:async()=>{throw Error("denied")}}})')
                self.no_production_controls()
        self.f.c.media.path_for(self.pid, media['object_id']).unlink()
        for width in ('desktop', 'narrow'):
            self.viewport(width)
            self.page.reload()
            expect(self.page.locator('#media-error')).to_be_visible()
            self.screenshot('missing-media')


if __name__ == '__main__':
    unittest.main()
