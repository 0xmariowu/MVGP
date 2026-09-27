"""Credential and human/reviewer authority boundary tests on real SQLite."""
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from production.auth import SYSTEM_PROJECT, AuthService, Principal
from production.contracts import DomainError
from production.store import Store


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "auth.sqlite")
        self.now = 1000.0
        self.auth = AuthService(self.store, "https://studio.example", clock=lambda: self.now)
        self.store.create_project("project_a", {}, "agent_a")
        self.store.create_project("project_b", {}, "agent_b")

    def tearDown(self):
        self.tmp.cleanup()

    def token(self, role="agent", **kwargs):
        return self.auth.provision_token("agent_a", role, ["project_a"], 100, **kwargs)

    def test_concurrent_first_auth_bootstrap_creates_one_internal_project(self):
        from contextlib import contextmanager
        from unittest.mock import patch

        store = Store(Path(self.tmp.name) / 'first.sqlite')
        transaction = store.transaction
        barrier = threading.Barrier(2)

        @contextmanager
        def synchronized(*, write=True):
            with transaction(write=write) as db:
                yield db
            if not write:
                barrier.wait(timeout=5)

        with patch.object(store, 'transaction', synchronized), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: AuthService(store, 'https://studio.example'), range(2)))
        self.assertEqual(len(results), 2)
        with store.transaction(write=False) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM projects WHERE project_id=?',
                                        (SYSTEM_PROJECT,)).fetchone()[0], 1)

    def owner_session(self, auth):
        import secrets as _secrets
        csrf = _secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            token = auth._issue("owner", "human", [], 600, "human-session", db,
                                csrf_hash=hashlib.sha256(csrf.encode()).hexdigest())
        return auth.authenticate(token, channel="cookie"), csrf

    def test_owner_session_sees_every_project_except_hidden_ones(self):
        # one owner; his desk shows all production projects but the ones he hid.
        auth = AuthService(self.store, "https://studio.example", clock=lambda: self.now, hidden_projects=["project_b"])
        owner, csrf = self.owner_session(auth)
        self.assertEqual(auth.visible_projects(owner), ["project_a"])
        auth.authorize(owner, "project_a", "human-decision", origin="https://studio.example", csrf_token=csrf)
        with self.assertRaises(DomainError):
            auth.authorize(owner, "project_b", "projects")

    def test_only_the_owner_login_session_holds_the_desk(self):
        # a human session from before the lean platform (an operator exchange or an
        # employee member) sees nothing and decides nothing, even before the cut-over revokes it.
        import secrets as _secrets
        auth = AuthService(self.store, "https://studio.example", clock=lambda: self.now)
        for actor, extra in (("f012-owner-walk", {}), ("member_1", {"member_id": "member_1"}), ("owner", {"member_id": "member_1"})):
            csrf = _secrets.token_urlsafe(32)
            with self.store.transaction() as db:
                token = auth._issue(actor, "human", ["project_a"], 600, "human-session", db,
                                    csrf_hash=hashlib.sha256(csrf.encode()).hexdigest(), **extra)
            other = auth.authenticate(token, channel="cookie")
            with self.subTest(actor=actor, extra=extra):
                self.assertEqual(auth.visible_projects(other), [])
                with self.assertRaises(DomainError):
                    auth.authorize(other, "project_a", "human-decision", origin="https://studio.example", csrf_token=csrf)

    def test_a_bearer_credential_can_never_make_a_human_decision(self):
        auth = AuthService(self.store, "https://studio.example", clock=lambda: self.now)
        for role in ("agent", "viewer", "worker"):
            with self.subTest(role=role):
                principal = auth.authenticate(auth.provision_token("svc_" + role, role, ["project_a"], 100))
                with self.assertRaises(DomainError):
                    auth.authorize(principal, "project_a", "human-decision", origin="https://studio.example", csrf_token="x" * 43)
        with self.assertRaises(DomainError):
            auth.provision_token("owner", "human", ["project_a"], 100)

    def test_agent_scope_and_revoke_on_cached_principal(self):
        principal = self.auth.authenticate(self.token())
        self.assertIsInstance(principal, Principal)
        self.auth.authorize(principal, "project_a", "prepare")
        with self.assertRaises(DomainError):
            self.auth.authorize(principal, "project_b", "prepare")
        self.auth.revoke(principal.credential_id)
        with self.assertRaises(DomainError):
            self.auth.authorize(principal, "project_a", "prepare")

    def test_queued_origin_requires_exact_live_maker_and_scope(self):
        token = self.token()
        origin = {'credential_id': token.split('.')[0], 'actor_id': 'agent_a', 'role': 'agent'}
        self.auth.require_agent_origin('project_a', origin)
        for change in ({'role': 'worker'}, {'actor_id': 'other'}, {'credential_id': 'missing'}):
            with self.subTest(change=change), self.assertRaises(DomainError):
                self.auth.require_agent_origin('project_a', {**origin, **change})
        for project in ('project_b', SYSTEM_PROJECT):
            with self.assertRaises(DomainError): self.auth.require_agent_origin(project, origin)
        self.auth.revoke(origin['credential_id'])
        with self.assertRaises(DomainError): self.auth.require_agent_origin('project_a', origin)
        other = self.token('viewer')
        with self.assertRaises(DomainError):
            self.auth.require_agent_origin('project_a', {**origin, 'credential_id': other.split('.')[0]})
        expired = self.token()
        self.now += 101
        with self.assertRaises(DomainError):
            self.auth.require_agent_origin('project_a', {**origin, 'credential_id': expired.split('.')[0]})

    def test_owner_sees_a_project_the_agent_creates(self):
        # one owner; a maker's new project is on his desk at once, with no membership record.
        auth = AuthService(self.store, 'https://studio.example', clock=lambda: self.now)
        human, csrf = self.owner_session(auth)
        maker = auth.authenticate(auth.provision_token('agent_owner', 'agent', [], 100, allow_create_project=True))
        with self.store.transaction() as db:
            self.store.create_project('owner_project', {}, maker.actor_id, conn=db)
            auth.grant_created_project(maker, 'owner_project', conn=db)
        self.assertIn('owner_project', auth.visible_projects(human))
        auth.authorize(human, 'owner_project', 'human-decision', origin='https://studio.example', csrf_token=csrf)
        self.assertEqual(auth.visible_projects(auth.authenticate(auth.provision_token('agent_owner', 'agent', [], 100,
                                              allow_create_project=True))), [])
        self.assertEqual(self.store.list_objects(SYSTEM_PROJECT, kind='project-membership'), [])

    def test_expiry_and_forged_principal(self):
        principal = self.auth.authenticate(self.token())
        for forged in (replace(principal, role="human"), replace(principal, project_ids=frozenset(["project_b"]))):
            with self.assertRaises(DomainError):
                self.auth.authorize(forged, "project_b", "projects")
        self.now = 1100
        with self.assertRaises(DomainError):
            self.auth.authorize(principal, "project_a", "prepare")
        with self.assertRaises(DomainError):
            self.auth.authenticate("bogus")

    def test_viewer_cannot_generate_and_system_project_hidden(self):
        principal = self.auth.authenticate(self.token("viewer"))
        self.auth.authorize(principal, "project_a", "media")
        for operation in ("submit", "prepare", "grant", "force", "human-decision"):
            with self.assertRaises(DomainError):
                self.auth.authorize(principal, "project_a", operation)
        with self.assertRaises(DomainError):
            self.auth.authorize(principal, SYSTEM_PROJECT, "artifacts")

    def test_agent_cannot_create_human_authority(self):
        token = self.token()
        self.assertFalse(hasattr(self.auth, "exchange_human"))  # no human exchange
        with self.assertRaises(DomainError):
            self.auth.provision_token("agent_a", "human", ["project_a"], 100)
        with self.assertRaises(DomainError):
            self.auth.authenticate(token, channel="cookie")
        with self.assertRaises(DomainError):
            self.auth.authorize(self.auth.authenticate(token), "project_a", "human-decision", origin="https://studio.example", csrf_token="pretend")

    def test_viewer_cookie_plays_media_without_production_or_human_authority(self):
        secret = self.token('viewer')
        with self.assertRaises(DomainError):
            self.auth.exchange_viewer(secret, 'https://evil.example')
        result = self.auth.exchange_viewer(secret, 'https://studio.example')
        viewer = self.auth.authenticate(result['session_token'], channel='cookie')
        self.auth.authorize(viewer, 'project_a', 'media')
        self.assertEqual(viewer.role, 'viewer')
        self.assertNotIn('csrf_token', result)
        for attribute in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(attribute, result['cookie'])
        with self.assertRaises(DomainError):
            self.auth.authenticate(result['session_token'])
        for operation in ('submit', 'prepare', 'human-decision', 'request-decision'):
            with self.assertRaises(DomainError):
                self.auth.authorize(viewer, 'project_a', operation,
                                    origin='https://studio.example', csrf_token='pretend')
        with self.assertRaises(DomainError):
            self.auth.authorize(viewer, 'project_b', 'media')
        self.assertNotIn(result['session_token'], str(self.store.list_objects(SYSTEM_PROJECT)))

    def test_viewer_session_inherits_parent_revocation_expiry_and_scope(self):
        for change in ('revoke', 'expire', 'scope'):
            secret = self.token('viewer')
            parent = self.auth.authenticate(secret)
            result = self.auth.exchange_viewer(secret, 'https://studio.example')
            viewer = self.auth.authenticate(result['session_token'], channel='cookie')
            record = self.store.get_object(SYSTEM_PROJECT, parent.credential_id)
            body = dict(record['body'])
            if change == 'revoke':
                body['revoked'] = True
            elif change == 'expire':
                body['expires_at'] = self.now
            else:
                body['project_ids'] = []
            self.store.append_revision(SYSTEM_PROJECT, parent.credential_id, record['revision'], body, 'operator')
            with self.assertRaises(DomainError):
                self.auth.authorize(viewer, 'project_a', 'media')
            with self.assertRaises(DomainError):
                self.auth.authenticate(result['session_token'], channel='cookie')

    def test_viewer_exchange_rejects_other_credentials_and_session_can_be_revoked(self):
        for role in ('agent', 'worker', 'reviewer'):
            kwargs = {'targets': ['sample_1'], 'review_task_id': 'task_1'} if role == 'reviewer' else {}
            with self.assertRaises(DomainError):
                self.auth.exchange_viewer(self.token(role, **kwargs), 'https://studio.example')
        result = self.auth.exchange_viewer(self.token('viewer'), 'https://studio.example')
        with self.assertRaises(DomainError):
            self.auth.exchange_viewer(result['session_token'], 'https://studio.example')
        principal = self.auth.authenticate(result['session_token'], channel='cookie')
        self.auth.revoke(principal.credential_id)
        with self.assertRaises(DomainError):
            self.auth.authorize(principal, 'project_a', 'media')

    def test_worker_scopes_and_no_author_escalation(self):
        worker = self.auth.authenticate(self.token("worker"))
        self.auth.authorize(worker, "project_a", "dispatch")
        for operation in ("prepare", "submit", "human-decision", "record-review"):
            with self.assertRaises(DomainError):
                self.auth.authorize(worker, "project_a", operation)
        with self.assertRaises(DomainError):
            self.auth.authorize(worker, "project_b", "dispatch")
        author = self.auth.authenticate(self.token())
        for operation in ("record-review", "issue-reviewer", "dispatch"):
            with self.assertRaises(DomainError):
                self.auth.authorize(author, "project_a", operation)
        with self.assertRaises(DomainError):
            self.token("reviewer", targets=["*"], review_task_id="task_1")

    def test_all_projects_scope_grows_with_new_projects_for_services_only(self):
        # one owner; the worker and the film service see every project, fresh on each check.
        worker = self.auth.authenticate(self.auth.provision_token("worker_1", "worker", [], 100, all_projects=True))
        film = self.auth.authenticate(self.auth.provision_token("film_1", "agent", [], 100, all_projects=True))
        self.assertEqual(self.auth.dispatch_scope(worker), ["project_a", "project_b"])
        self.store.create_project("project_c", {}, "agent_a")
        self.assertEqual(self.auth.dispatch_scope(worker), ["project_a", "project_b", "project_c"])
        self.auth.authorize(worker, "project_c", "dispatch")
        self.auth.authorize(film, "project_c", "prepare")
        with self.assertRaises(DomainError):
            self.auth.authorize(worker, SYSTEM_PROJECT, "dispatch")
        with self.assertRaises(DomainError):
            self.auth.authorize(worker, "project_c", "human-decision")
        for role, kwargs in (("agent", {"allow_create_project": True}), ("viewer", {}), ("reviewer",
                             {"targets": ["x"], "review_task_id": "t1"})):
            with self.subTest(role=role), self.assertRaises(DomainError):
                self.auth.provision_token("x_1", role, [], 100, all_projects=True, **kwargs)
        plain = self.auth.authenticate(self.token("worker"))
        self.assertEqual(self.auth.dispatch_scope(plain), ["project_a"])

    def test_reviewer_exact_target_task_and_no_generation(self):
        token = self.token("reviewer", targets=["shot_1@2"], review_task_id="review_task_1")
        principal = self.auth.authenticate(token)
        self.auth.authorize(principal, "project_a", "review-evidence", target="shot_1@2")
        self.auth.authorize(principal, "project_a", "record-review", target="review_task_1")
        for op, target in (("review-evidence", "shot_1@1"), ("review-evidence", None), ("record-review", "other_task"), ("submit", "shot_1@2"), ("artifacts", "shot_1@2"), ("human-decision", None)):
            with self.assertRaises(DomainError):
                self.auth.authorize(principal, "project_a", op, target=target)
        with self.assertRaises(DomainError):
            self.token("reviewer")

    def test_hash_only_persistence_and_atomic_revoke(self):
        token = self.token()
        credential = self.auth.authenticate(token)
        serialized = str(self.store.list_objects(SYSTEM_PROJECT)) + str(self.store.events(SYSTEM_PROJECT))
        self.assertNotIn(token, serialized)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), serialized)
        with self.assertRaises(RuntimeError), self.store.transaction() as conn:
            self.auth.revoke(credential.credential_id, conn=conn)
            raise RuntimeError("rollback")
        self.auth.authorize(credential, "project_a", "prepare")

    def test_create_project_capability_does_not_grant_others(self):
        ordinary = self.auth.authenticate(self.token())
        with self.assertRaises(DomainError):
            self.auth.authorize(ordinary, None, "create-project")
        capable = self.auth.authenticate(self.token(allow_create_project=True))
        self.auth.authorize(capable, None, "create-project")
        with self.assertRaises(DomainError):
            self.auth.grant_created_project(capable, "project_b")
        with self.store.transaction() as conn:
            self.store.create_project("project_new", {}, capable.actor_id, conn=conn)
            self.auth.grant_created_project(capable, "project_new", conn=conn)
        self.auth.authorize(capable, "project_new", "prepare")


if __name__ == "__main__":
    unittest.main()
