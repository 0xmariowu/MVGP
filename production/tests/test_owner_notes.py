"""The owner's notes are platform records only his human session writes."""
import tempfile
import unittest
from pathlib import Path

from production.auth import AuthService
from production.contracts import DomainError, ObjectRef
from production.owner_notes import KIND, OwnerNoteRequest, OwnerNotes, OwnerNoteWithdraw
from production.store import Store
from production.tests.fixtures import owner_session

ORIGIN = 'https://studio.example'


class OwnerNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'notes.sqlite')
        self.auth = AuthService(self.store, ORIGIN)
        self.store.create_project('project_1', {}, 'operator')
        self.shot = self.store.create_object('project_1', 'shot', {'content': {'shot': 'S01-010A'}}, 'author')
        self.take = self.store.create_object('project_1', 'media', {'media_type': 'video/mp4'}, 'worker_service')
        session = owner_session(self.auth)
        self.csrf = session['csrf_token']
        self.human = self.auth.authenticate(session['session_token'], channel='cookie')
        self.notes = OwnerNotes(self.store, self.auth)

    def ref(self, obj):
        return ObjectRef(object_id=obj['object_id'], revision=obj['revision'], digest=obj['digest'])

    def note(self, key='n1', **values):
        return OwnerNoteRequest(**{'idempotency_key': key, 'target': self.ref(self.shot), 'take': self.ref(self.take),
                                   'at_seconds': 2.5, 'text': '她转头太快了', 'csrf_token': self.csrf, **values})

    def test_the_owner_writes_a_note_on_an_exact_take_and_withdraws_it(self):
        note = self.notes.add(self.human, 'project_1', self.note(), origin=ORIGIN)
        self.assertEqual(note['kind'], KIND)
        self.assertEqual(note['body']['take'], self.ref(self.take).model_dump())
        self.assertEqual(note['body']['at_seconds'], 2.5)
        self.assertEqual(self.notes.add(self.human, 'project_1', self.note(), origin=ORIGIN)['object_id'], note['object_id'])
        withdrawn = self.notes.withdraw(self.human, 'project_1', OwnerNoteWithdraw(
            idempotency_key='w1', note_id=note['object_id'], expected_revision=1, csrf_token=self.csrf), origin=ORIGIN)
        self.assertTrue(withdrawn['body']['withdrawn'])
        self.assertEqual(withdrawn['revision'], 2)

    def test_no_agent_viewer_other_origin_or_wrong_csrf_can_write(self):
        agent = self.auth.authenticate(self.auth.provision_token('maker', 'agent', ['project_1'], 300))
        viewer = self.auth.authenticate(self.auth.provision_token('watcher', 'viewer', ['project_1'], 300))
        for principal in (agent, viewer):
            with self.subTest(role=principal.role), self.assertRaises(DomainError):
                self.notes.add(principal, 'project_1', self.note(key='x-' + principal.role), origin=ORIGIN)
        with self.assertRaises(DomainError):
            self.notes.add(self.human, 'project_1', self.note(key='o1'), origin='https://evil.example')
        with self.assertRaises(DomainError):
            self.notes.add(self.human, 'project_1', self.note(key='c1', csrf_token='x' * 32), origin=ORIGIN)
        self.assertEqual(self.store.list_objects('project_1', kind=KIND), [])

    def test_a_note_names_exact_versions(self):
        stale = ObjectRef(object_id=self.shot['object_id'], revision=1, digest='0' * 64)
        with self.assertRaises(DomainError):
            self.notes.add(self.human, 'project_1', self.note(key='s1', target=stale), origin=ORIGIN)
        with self.assertRaises(DomainError):  # a take must be media
            self.notes.add(self.human, 'project_1', self.note(key='s2', take=self.ref(self.shot)), origin=ORIGIN)


if __name__ == '__main__':
    unittest.main()
