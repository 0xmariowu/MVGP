"""Rehearsal helpers: the whole platform on a restored copy of live, spending nothing.

`tools/rehearsal/rehearse.py prepare` restores the copy, writes the lean configs and the rehearsal owner identity;
this module starts it (`rehearse.up`), drives the agent through the real CLI and the owner through the real desk in a
headless Chrome (signed in by the rehearsal Access identity, as Cloudflare Access signs the owner in live), and reads
the copy's records. It refuses any database outside `<studio>/rehearsal/`, so nothing here can touch live.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tools.rehearsal import rehearse  # noqa: E402

ORIGIN = rehearse.ORIGIN
MAKING = ('queued', 'dispatching', 'submitted', 'running')
UNVERIFIED = ssl._create_unverified_context()  # the rehearsal's own self-signed certificate


class Rehearsal:
    """One rehearsal round: the prepared copy, the run id, and where the evidence goes."""

    def __init__(self, studio: Path, *, evidence: Path, run: str | None = None) -> None:
        self.paths = rehearse.Paths(studio)
        self.studio = self.paths.studio
        self.run = run or time.strftime('%H%M%S')
        self.evidence_path = Path(evidence)
        self.state_path = self.paths.root / 'loop-state.json'
        current = json.loads(self.paths.current.read_text())
        self.runtime = Path(current['code_dir'])
        self.api = self.config(Path(current['api_config']))
        worker = self.config(Path(current['worker_config']))
        self.python = str(self.studio / 'venv/bin/python')
        self.ffmpeg = str(Path(worker['ffmpeg_path']).parent)
        self.maker_token = self.paths.config / 'credentials/maker.token'
        self.films: dict[str, str] = {}
        self._pw = None

    # ------------------------------------------------------------ safety and records
    def config(self, path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text())
        self.paths.inside(Path(value['storage']['database']))
        return value

    @staticmethod
    def private(path: Path, value: Any) -> Path:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
        return path

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def save(self, **values: Any) -> None:
        self.private(self.state_path, {**self.state(), **values})

    def check(self, step: str, ok: bool, detail: Any) -> bool:
        data = json.loads(self.evidence_path.read_text()) if self.evidence_path.exists() else {'run': self.run, 'checks': []}
        data['checks'] = [c for c in data['checks'] if c['step'] != step] + [{'step': step, 'ok': bool(ok), 'detail': detail}]
        self.private(self.evidence_path, data)
        print(('PASS ' if ok else 'FAIL ') + step, json.dumps(detail, ensure_ascii=False, default=str)[:400], flush=True)
        return bool(ok)

    def skip(self, step: str, reason: str) -> None:
        data = json.loads(self.evidence_path.read_text()) if self.evidence_path.exists() else {'run': self.run, 'checks': []}
        data['checks'] = [c for c in data['checks'] if c['step'] != step] + [
            {'step': step, 'ok': True, 'skipped': True, 'detail': reason}]
        self.private(self.evidence_path, data)
        print('SKIP ' + step + ': ' + reason, flush=True)

    # ------------------------------------------------------------ services
    def up(self, *, complete_delay: float = 5.0) -> None:
        rehearse.up(self.studio, complete_delay=complete_delay)

    def down(self) -> None:
        rehearse.down(self.studio)
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    # ------------------------------------------------------------ the agent: the real CLI against the API
    def maker(self) -> str:
        return self.maker_token.read_text().strip()

    def cli(self, *args: str, body: Any = None) -> tuple[int, Any]:
        import httpx
        from production import cli as cli_mod

        out, err = io.StringIO(), io.StringIO()
        code = cli_mod.main([*args, *(['--input', '-'] if body is not None else [])],
                            environ={'MVGP_URL': ORIGIN, 'MVGP_TOKEN': self.maker()},
                            stdin=io.StringIO(json.dumps(body) if body is not None else ''), stdout=out, stderr=err,
                            transport=httpx.HTTPTransport(verify=False))
        text = out.getvalue() or err.getvalue()
        assert self.maker() not in text
        return code, json.loads(text)

    def http(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        req = urllib.request.Request(f'{ORIGIN}{path}', method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + self.maker(), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=600, context=UNVERIFIED) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    # ------------------------------------------------------------ read-only store views
    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.api['storage']['database']}?mode=ro", uri=True)

    @staticmethod
    def ref(o: dict[str, Any]) -> dict[str, Any]:
        return {k: o[k] for k in ('object_id', 'revision', 'digest')}

    def current(self, pid: str, oid: str) -> dict[str, Any]:
        row = self.db().execute("SELECT r.body, r.revision, r.digest FROM objects o JOIN revisions r ON o.project_id=r.project_id "
                                "AND o.object_id=r.object_id AND o.current_revision=r.revision WHERE o.project_id=? AND o.object_id=?",
                                (pid, oid)).fetchone()
        return {'object_id': oid, 'revision': row[1], 'digest': row[2], 'body': json.loads(row[0])}

    def objects(self, pid: str, kind: str) -> list[dict[str, Any]]:
        rows = self.db().execute("SELECT o.object_id, r.revision, r.digest, r.author, r.body FROM objects o JOIN revisions r ON "
                                 "o.project_id=r.project_id AND o.object_id=r.object_id AND o.current_revision=r.revision "
                                 "WHERE o.project_id=? AND o.kind=?", (pid, kind)).fetchall()
        return [{'object_id': a, 'revision': b, 'digest': c, 'author': d, 'body': json.loads(e)} for a, b, c, d, e in rows]

    def budget(self, pid: str, key: str) -> dict[str, Any] | None:
        row = self.db().execute('SELECT ceiling, reserved, spent, unit FROM budgets WHERE project_id=? AND budget_key=?', (pid, key)).fetchone()
        return dict(zip(('ceiling', 'reserved', 'spent', 'unit'), row)) if row else None

    def shot_card(self, pid: str, label: str) -> dict[str, Any] | None:
        return next((o for o in self.objects(pid, 'shot') if o['body']['content'].get('shot') == label), None)

    def wait(self, test, *, limit: int = 240, every: float = 3.0):  # noqa: ANN001, ANN201
        """Bounded wait on real records (never a count of earlier runs)."""
        for _ in range(limit):
            value = test()
            if value:
                return value
            time.sleep(every)
        return None

    def jobs_idle(self, pid: str) -> bool:
        return not [j for j in self.objects(pid, 'job') if j['body'].get('state') in MAKING]

    def offers(self, pid: str, label: str) -> list[dict[str, Any]]:
        return [r for r in self.objects(pid, 'decision-request') if r['author'] == 'decision_service'
                and r['body'].get('purpose') == 'take' and r['body'].get('state') == 'pending'
                and self.current(pid, r['body']['target']['object_id'])['body']['content'].get('shot') == label]

    # ------------------------------------------------------------ the writer
    @staticmethod
    def prompt_for(card: dict[str, Any]) -> str:
        """The writer's whole prompt for a rehearsal card (rehearsal text, not a craft claim):
        bare reference lines, so the platform pastes the registry descriptors, then the writer's own blocks."""
        content = card['body']['content'] if 'body' in card else card
        m, d = content.get('The material', {}), content.get('Direction', {})
        action = str(m.get('the action in one to three sentences') or '').strip()
        seconds = int(m.get('the running time in seconds') or 5)
        goal = str(d.get('the goal of the shot in one line') or '').strip()
        people = [str(t).lstrip('@') for t in m.get('everyone in frame with their tags and state variants', [])]
        props = [str(t).lstrip('@') for t in m.get('props and vehicles with tags', [])]
        place = re.findall(r'@([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)', str(m.get('the location and INT/EXT with the asset that covers it') or ''))
        blocks = ['ACTIVE REFERENCES\n' + '\n'.join('@' + t for t in dict.fromkeys([*people, *props, *place])),
                  f'FIRST FRAME AND SPATIAL BLOCKING\nFrame one: {action.split(".")[0]}. Everyone holds their mark.',
                  'OPTICS\nEye-level medium view, 35mm, held for the whole shot.',
                  'CAMERA\nLocked off, real-time speed.',
                  f'ACTION TIMING\n0.0–{seconds:.1f}s — {action or goal}',
                  'PHYSICS\nCloth and hair settle with weight; nothing floats or snaps.',
                  'LIGHTING\nOne hard source from the window side, deep fill on the far side.']
        if people:
            blocks.append('CHARACTER ACTING\n' + '\n'.join(f'@{t} holds the look a beat longer than is comfortable; breath shows '
                                                          'before any line.' for t in people))
        return '\n\n'.join(blocks)

    def order(self, pid: str, cards: list[dict[str, Any]], key: str, task: str = 'shot') -> tuple[int, Any]:
        return self.http('POST', f'/v1/projects/{pid}/shoot-orders',
                         {'idempotency_key': key, 'cards': [self.ref(c) for c in cards], 'takes': 4, 'task': task})

    def write_and_shoot(self, pid: str, label: str, manuals: Path, task: str = 'shot') -> tuple[int, dict[str, Any]]:
        """Take the manuals, name them on the card, write the whole prompt (unless the card has one), order four takes."""
        code, taken = self.cli('playbook', pid, '--dir', str(manuals))
        assert code == 0, taken
        card = self.shot_card(pid, label)
        production = {**card['body']['content']['_production'], 'playbook_version': taken['playbook_version']}
        production.setdefault('prompt', self.prompt_for(card))
        if production != card['body']['content']['_production']:
            code, done = self.cli('patch', pid, body={'idempotency_key': f'r-v-{label}-{self.run}-{card["revision"]}',
                                  'expected_revision': card['revision'], 'target': self.ref(card),
                                  'creative_path': ['content', '_production'], 'value': production,
                                  'reason': 'The writer wrote the whole prompt from the manuals and names their version.'})
            assert code == 0, done
        card = self.shot_card(pid, label)
        code, probe = self.order(pid, [card], f'r-fire-{label}-{self.run}-{card["revision"]}', task)
        return code, {c['shot']: (c.get('stage'), c.get('reason')) for c in probe.get('cards', [])}

    # ------------------------------------------------------------ the owner: the real desk in headless Chrome
    def browser(self, pids: list[str]):  # noqa: ANN201
        """The owner on the real desk: Access puts his signed identity on every request (the rehearsal key signs it)."""
        from playwright.sync_api import sync_playwright
        if self._pw is None:  # one engine per process
            self._pw = sync_playwright().start()
        b = self._pw.chromium.launch(headless=True, channel='chrome')  # real Chrome plays H.264
        context = b.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 900},
                                extra_http_headers={'cf-access-jwt-assertion': rehearse.owner_jwt(self.studio)})
        page = context.new_page()
        errors: list[str] = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(f'{ORIGIN}/review')
        # The desk exchanges the Access identity silently; the login card shows only when that fails.
        page.wait_for_function("() => !document.getElementById('login').hidden || document.querySelector('.project')",
                               timeout=30000)
        if page.locator('#login').is_visible():
            b.close()
            raise RuntimeError('the rehearsal owner identity was not accepted by the desk')
        return b, page, errors

    def agent_cannot_decide(self, pid: str) -> int:
        """An agent bearer on the owner's decision route: the platform must refuse it (403), whatever the body."""
        code, _ = self.http('POST', f'/v1/projects/{pid}/human-decisions', {
            'idempotency_key': f'agent-decides-{self.run}', 'request_id': 'decision_' + '0' * 32, 'target_hash': '0' * 64,
            'choice': 'confirm', 'csrf_token': 'x' * 32})
        return code

    def open_project(self, page, title: str) -> None:  # noqa: ANN001
        page.locator('.project', has_text=title).first.click()
        page.wait_for_timeout(2500)

    @staticmethod
    def card(page, label: str):  # noqa: ANN001, ANN205
        return page.locator('.card', has=page.locator('.card-name', has_text=label)).first

    def owner_pick(self, pid: str, title: str, label: str, take: int) -> tuple[int, str, list[str]]:
        b, page, errors = self.browser([pid])
        try:
            self.open_project(page, title)
            c = self.card(page, label)
            c.wait_for(timeout=30000)
            c.locator('.takes > .take:not(.source)').nth(take).hover()  # a recreation card leads with the source clip
            with page.expect_response(lambda r: r.url.endswith('/human-decisions'), timeout=30000) as resp:
                c.get_by_role('button', name='用这条', exact=True).nth(0).click()
            try:
                c.locator('.status', has_text='定了').wait_for(timeout=20000)
            except Exception:  # noqa: BLE001 -- the status text is the evidence either way
                pass
            status = c.locator('.status').inner_text()
            page.screenshot(path=str(self.studio / f'evidence/rehearsal-{label}-pick.png'))
            return resp.value.status, status, errors
        finally:
            b.close()

    def owner_switch(self, pid: str, title: str, name: str, on: bool) -> tuple[bool | None, list[str]]:
        """The owner sets a film's switch in 个人设置 on the real desk (样片模式)."""
        b, page, errors = self.browser([pid])
        try:
            self.open_project(page, title)
            page.locator('#open-settings').click()
            box = page.locator(f'#steps input[name="{name}"]')
            box.wait_for(timeout=30000)
            if box.is_checked() != on:
                with page.expect_response(lambda r: r.url.endswith('/switches') and r.request.method == 'POST', timeout=30000):
                    box.click()
                page.wait_for_timeout(500)
            code, found = self.http('GET', f'/v1/projects/{pid}/switches')
            return (found.get('switches') or {}).get(name) if code == 200 else None, errors
        finally:
            b.close()

    def owner_rebatch(self, pid: str, title: str, label: str, sentence: str) -> tuple[int, list[str]]:
        b, page, errors = self.browser([pid])
        try:
            self.open_project(page, title)
            c = self.card(page, label)
            c.wait_for(timeout=30000)
            c.get_by_role('button', name='再拍一批', exact=True).click()
            box = c.locator('.note-box input')
            box.fill(sentence)
            with page.expect_response(lambda r: r.url.endswith('/human-decisions'), timeout=30000) as resp:
                box.press('Enter')
            page.wait_for_timeout(1500)
            return resp.value.status, errors
        finally:
            b.close()

    def owner_film(self, pid: str, title: str, *, confirm: bool = True) -> dict[str, Any]:
        b, page, errors = self.browser([pid])
        try:
            self.open_project(page, title)
            page.locator('#toggle-cut').click()
            approve = page.locator('#cut-approve')
            approve.wait_for(timeout=30000)
            page.wait_for_timeout(2000)
            page.screenshot(path=str(self.studio / f'evidence/rehearsal-{pid[-6:]}-film.png'))
            src = page.locator('#cut-video').get_attribute('src') or ''
            enabled, code = approve.is_enabled(), None
            if enabled and confirm:
                with page.expect_response(lambda r: r.url.endswith('/human-decisions'), timeout=30000) as resp:
                    approve.click()
                code = resp.value.status
                page.wait_for_timeout(1500)
            return {'enabled': enabled, 'code': code, 'status': page.locator('#cut-status').inner_text(), 'src': src, 'errors': errors}
        finally:
            b.close()

    def owner_reason(self, pid: str, label: str) -> tuple[int, str | None]:
        """What the agent reads back: the owner's sentence on his latest decision for this shot, through the CLI."""
        shot = self.shot_card(pid, label)
        receipts = [r for r in self.objects(pid, 'human-receipt') if (r['body'].get('target') or {}).get('object_id') == shot['object_id']]
        seq = {oid: n for oid, n in self.db().execute("SELECT json_extract(body,'$.object_id'), sequence FROM events WHERE "
                                                      "project_id=? AND kind='object.created'", (pid,))}
        latest = max(receipts, key=lambda r: seq.get(r['object_id'], 0))
        code, seen = self.cli('read', pid, latest['object_id'])
        return code, (seen.get('details') or {}).get('reason')
