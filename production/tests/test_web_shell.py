"""Viewer surface follows the agreed read/inspect/copy scope."""
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'web'


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.items = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


class WebShellTests(unittest.TestCase):
    def test_accessible_two_levels_and_only_allowed_controls(self):
        text = (ROOT / 'index.html').read_text()
        items = Elements(text).items
        ids = [a['id'] for _, a in items if 'id' in a]
        self.assertEqual(len(ids), len(set(ids)))
        for required in ('projects-view', 'project-view', 'plan-panel', 'results-panel', 'files-panel',
                         'video', 'image', 'audio', 'versions', 'copy-reference', 'copy-fallback', 'decision-view'):
            self.assertIn(required, ids)
        self.assertIn('zh-CN', text)
        self.assertEqual([a['id'] for t, a in items if t == 'form'], ['login-form'])
        self.assertTrue(all(a.get('readonly') is not None or 'readonly' in a
                            for t, a in items if t == 'textarea'))
        self.assertTrue(all(a['type'] == 'password' for t, a in items if t == 'input'))
        self.assertFalse(any('contenteditable' in a for _, a in items))
        for _, attrs in items:
            self.assertFalse(any(k.startswith('on') for k in attrs))
        scripts = [a for t, a in items if t == 'script']
        self.assertEqual(scripts, [{'src': '/viewer/app.js', 'defer': None}])
        self.assertFalse(any(t == 'style' for t, _ in items))
        self.assertNotIn('点击生成', text)
        self.assertIn('aria-live="polite"', text)
        self.assertTrue(all('controls' in a for t, a in items if t in ('video', 'audio')))


if __name__ == '__main__':
    unittest.main()


class ReviewDeskShellTests(unittest.TestCase):
    """The review desk (看片台) is static, CSP-clean and mutates only through the human-decision endpoint."""

    def test_review_page_has_no_inline_code_and_loads_only_its_own_files(self):
        text = (ROOT / 'review.html').read_text()
        items = Elements(text).items
        ids = [a['id'] for _, a in items if 'id' in a]
        self.assertEqual(len(ids), len(set(ids)))
        for required in ('projects', 'feed-inner', 'cut-view', 'light', 'employee-login', 'copy-all', 'toggle-cut'):
            self.assertIn(required, ids)
        self.assertIn('zh-CN', text)
        for _, attrs in items:
            self.assertFalse(any(k.startswith('on') or k == 'style' for k in attrs))
        # the project view opens inside the desk's main area.
        self.assertEqual([a for t, a in items if t == 'script'], [{'src': '/viewer/project.js'}, {'src': '/viewer/review.js'}])
        self.assertEqual([a['href'] for t, a in items if t == 'link'], ['/viewer/review.css', '/viewer/project.css'])
        self.assertIn('project-view', ids)
        self.assertFalse(any(t == 'style' for t, _ in items))

    def test_review_styles_have_the_source_tile_row(self):
        # a recreation card leads with the source segment.
        css = (ROOT / 'review.css').read_text()
        self.assertIn('.takes.src{grid-template-columns:repeat(5,minmax(0,1fr))}', css)
        self.assertIn('.take.source', css)

    def test_review_script_posts_only_session_and_human_decisions(self):
        import re
        script = (ROOT / 'review.js').read_text()
        posted = set(re.findall(r"api\(`?'?([^`',]+)[`']?, ", script)) - {'path'}  # minus the helper's own signature
        # The owner's own writes: decisions, the 拍片步骤 switches and his desk notes
        # all require a session + CSRF.
        self.assertEqual({path for path in posted if not path.startswith('/v1/projects/${seg(pid)}')
                          and not path.startswith('/v1/projects/${seg(S.pid)}/human-decisions')
                          and not path.startswith('/v1/projects/${seg(S.pid)}/owner-notes')
                          and path != '/v1/projects/${seg(S.pid)}/switches'},
                         {'/v1/session/access', '/v1/session/logout'})  # no human exchange
        self.assertIn('/human-decisions', script)
        for forbidden in ('/submissions', '/batches', '/cuts', '/reviews', '/decision-requests', 'innerHTML', 'eval('):
            self.assertNotIn(forbidden, script)


class ProjectPageShellTests(unittest.TestCase):
    """The project page is static, CSP-clean and read-only; the view is built by project.js."""

    def test_project_page_has_no_inline_code_and_loads_only_its_own_files(self):
        text = (ROOT / 'project.html').read_text()
        items = Elements(text).items
        ids = [a['id'] for _, a in items if 'id' in a]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('pj-root', ids)  # project.js builds the view in place
        for _, attrs in items:
            self.assertFalse(any(k.startswith('on') or k == 'style' for k in attrs))
        self.assertEqual([a for t, a in items if t == 'script'], [{'src': '/viewer/project.js'}])
        self.assertEqual([a['href'] for t, a in items if t == 'link'], ['/viewer/project.css'])
        self.assertFalse(any(t == 'style' for t, _ in items))

    def test_project_script_only_reads(self):
        script = (ROOT / 'project.js').read_text()
        # Its one POST is the silent session renewal; it never writes production data.
        self.assertEqual(script.count("method: 'POST'"), 1)
        self.assertIn("fetch('/v1/session/access', {method: 'POST'", script)
        for forbidden in ('innerHTML', 'eval(', 'outerHTML', 'insertAdjacentHTML', '/human-decisions', '/owner-notes'):
            self.assertNotIn(forbidden, script)
        self.assertIn('/project-tree', script)
