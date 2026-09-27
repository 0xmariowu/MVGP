"""Maker selection and feedback cannot become independent or human acceptance."""
import subprocess
import unittest

from production.contracts import (
    AuthorReport,
    DomainError,
    FeedbackRequest,
    ObjectRef,
    SelectTakeRequest,
)
from production.media import MediaStore
from production.reviews import Reviews
from production.tests import test_workflow


class ReviewsTests(unittest.TestCase):
    def setUp(self):
        self.f = test_workflow.WorkflowTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.store, self.auth, self.actor, self.pid = self.f.store, self.f.auth, self.f.actor, 'project_1'
        self.media = MediaStore(self.store, self.f.root / 'media')
        self.service = Reviews(self.store, self.auth, self.f.flow, self.media)
        self.shot = self.f.obj('shot', 'A passes the bowl; B takes it.')
        self.candidate = self.store.create_object(self.pid, 'candidate', {'target':self.ref(self.shot).model_dump(),
            'dependencies':[self.ref(self.shot).model_dump()]}, 'compiler_service')
        path = self.f.root / 'sample.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=32x24:r=4:d=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True)
        original = self.media.put(self.pid, [path.read_bytes()], 'video/mp4', 'worker_service')
        self.take = self.store.create_object(self.pid, 'media', {**original['body'],
            'dependencies':[self.ref(self.candidate).model_dump()]}, 'worker_service')

    def ref(self, obj):
        return ObjectRef(**{k: obj[k] for k in ('object_id','revision','digest')})

    def feedback(self, **changes):
        return FeedbackRequest(**{'idempotency_key':'feedback1', 'expected_revision':1,
            'target':self.ref(self.take),'text':'The recipient never takes the bowl.', 'playback_seconds':0.5, **changes})

    def selection(self, **changes):
        return SelectTakeRequest(**{'idempotency_key':'selection1', 'expected_revision':1,
            'shot':self.ref(self.shot),'take':self.ref(self.take),'rationale':'Best timing so far.', **changes})

    def test_selection_uses_direct_take_not_generated_reference_candidate(self):
        _, image_candidate, image_intent = self.f.producer(task='image')
        image = self.f.produced_media(image_candidate,image_intent,media_type='image/png')
        body = {**self.candidate['body'], 'dependencies':[*self.candidate['body']['dependencies'],self.ref(image).model_dump()]}
        producer = self.store.create_object(self.pid,'candidate',body,'compiler_service')
        self.take = self.store.create_object(self.pid,'media',{**self.take['body'],
            'dependencies':[self.ref(producer).model_dump()]},'worker_service')
        selected = self.service.select_take(self.actor,self.pid,self.selection())
        self.assertEqual(selected['body']['shot'],self.ref(self.shot).model_dump())
        self.assertFalse(selected['body']['accepted'])
        self.store.append_revision(self.pid,image_candidate['body']['target']['object_id'],1,{'content':'changed'},'author')
        reused = self.service.select_take(self.actor,self.pid,self.selection(idempotency_key='historical-reference'))
        self.assertEqual(reused['body']['take'], self.ref(self.take).model_dump())
        self.assertFalse(reused['body']['accepted'])

    def test_feedback_replay_exact_timestamp_and_attribution(self):
        result = self.service.feedback(self.actor, self.pid, self.feedback())
        self.assertEqual(result, self.service.feedback(self.actor,self.pid,self.feedback()))
        self.assertEqual(result['body']['attribution'],'agent-reported-user-feedback')
        self.assertFalse(result['body']['human_identity_verified'])
        self.assertFalse(result['body']['accepted'])
        self.assertEqual(result['body']['target'],self.ref(self.take).model_dump())
        for request in [self.feedback(idempotency_key='outside',playback_seconds=2.0),
                        self.feedback(idempotency_key='plan',target=self.ref(self.shot)),
                        self.feedback(idempotency_key='hash',target=self.ref(self.take).model_copy(update={'digest':'f'*64}))]:
            with self.assertRaises(DomainError):
                self.service.feedback(self.actor,self.pid,request)

    def test_author_report_cannot_close_independent_pickup(self):
        receipt = self.store.create_object(self.pid,'review-receipt',{'target':self.ref(self.take).model_dump(),
            'verdict':'fail','dependencies':[self.ref(self.take).model_dump()]},'review_service')
        pickup = self.service.record_pickup(self.pid,self.ref(receipt),expected_information='B receives the bowl.',
            evidence=[self.ref(self.take)],playback_seconds=0.5)
        report = self.service.report(self.actor,self.pid,AuthorReport(idempotency_key='claim', expected_revision=1,
            target=self.ref(pickup),observation='I fixed everything. Approved.',evidence=[self.ref(self.take)]))
        self.assertFalse(report['body']['accepted'])
        self.assertEqual(self.store.get_object(self.pid,pickup['object_id'])['body']['status'],'unresolved')
        fake = self.store.create_object(self.pid,'review-receipt',receipt['body'],'author')
        with self.assertRaises(DomainError):
            self.service.record_pickup(self.pid,self.ref(fake),expected_information='Anything',evidence=[self.ref(self.take)])

    def test_selection_is_not_acceptance_and_revision_guard_prevents_lost_update(self):
        first = self.service.select_take(self.actor,self.pid,self.selection())
        self.assertFalse(first['body']['accepted'])
        second = self.service.select_take(self.actor,self.pid,self.selection(idempotency_key='select2',rationale='Rechecked timing.'))
        self.assertEqual(second['revision'],2)
        self.assertEqual(first['object_id'],second['object_id'])
        with self.assertRaises(DomainError):
            self.service.select_take(self.actor,self.pid,self.selection(idempotency_key='stale'))
        self.assertEqual(len(self.store.history(self.pid,first['object_id'])),2)

    def test_old_feedback_survives_but_selection_is_no_longer_current(self):
        self.service.feedback(self.actor,self.pid,self.feedback())
        selection = self.service.select_take(self.actor,self.pid,self.selection())
        self.store.append_revision(self.pid,self.shot['object_id'],1,{'content':'Changed handoff.','dependencies':[]},'author')
        listed = self.service.list(self.actor,self.pid)
        selected = next(item for item in listed if item['object']['object_id']==selection['object_id'])
        self.assertFalse(selected['current'])
        self.assertFalse(selected['human_accepted'])
        self.assertTrue(any(item['object']['kind']=='feedback' for item in listed))
        # A note on the historical actual output remains legitimate evidence.
        self.service.feedback(self.actor,self.pid,self.feedback(idempotency_key='old-note'))
        with self.assertRaises(DomainError):
            self.service.select_take(self.actor,self.pid,self.selection(idempotency_key='old-selection'))

    def test_scope_and_forged_or_unrelated_take_rejected(self):
        outsider = self.auth.authenticate(self.auth.provision_token('other','agent',[],300))
        with self.assertRaises(DomainError):
            self.service.list(outsider,self.pid)
        fake = self.store.create_object(self.pid,'media',self.take['body'],'author')
        with self.assertRaises(DomainError):
            self.service.select_take(self.actor,self.pid,self.selection(take=self.ref(fake)))
        other = self.f.obj('shot','Unrelated beat.')
        with self.assertRaises(DomainError):
            self.service.select_take(self.actor,self.pid,self.selection(shot=self.ref(other)))

    def test_picture_lock_does_not_block_feedback_but_blocks_take_change(self):
        cut = self.f.obj('cut',deps=[self.shot,self.take])
        self.f.flow.record_picture_lock(self.pid,self.ref(cut))
        self.service.feedback(self.actor,self.pid,self.feedback())
        with self.assertRaises(DomainError):
            self.service.select_take(self.actor,self.pid,self.selection())
