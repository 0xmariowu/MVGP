"""Craft checkpoint: plumbing, never a claim of scene comprehension.

The independently authored source is a blue delivery cart passing a red roadside
marker, followed by a view in which it remains ahead. Two image assets (road with
marker; cart) feed adjacent mvgp-video-v1 stress probes. The fake second take
incorrectly puts the cart behind again. A pixel oracle records that discrepancy;
a scoped authored repair produces a synthetic corrected take and a rough cut.

Policy, prompt and capability documents come unchanged from the shipped bundle;
the standards route selects its retained chat profile for these fake reviewers.
Method adoption below is explicitly SYNTHETIC TEST EVIDENCE. It
cannot qualify production assets or certify formal shots, director acceptance,
1080p source detail, live spending, or artistic quality. Those require separate live and artistic review.
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw

from production.asset_methods import AssetMethods
from production.auth import AuthService
from production.batches import Batches
from production.compiler import Compiler
from production.context import ContextService
from production.contracts import (
    ArtifactRevision,
    BatchRequest,
    MethodSelection,
    ObjectRef,
    ObserveRequest,
    PrepareRequest,
    ProjectCreate,
    canonical_json,
)
from production.cuts import Cuts
from production.gates import Gates
from production.jobs import Download, Jobs, SafeDownloader
from production.media import MediaStore
from production.patches import Patches
from production.projects import Projects
from production.reader import ENDPOINT, Reader, ReaderResponse
from production.runtime_config import RuntimeConfig
from production.reviews import Reviews
from production.store import Store
from production.submissions import Submissions
from production.worker import Worker
from production.workflow import Workflow
from production.tests import writer_fixture
from production.tests.fixtures import FakeProvider, hf_era_document, runtime_config

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ('A blue delivery cart travels east along a straight road. It begins west of a red roadside marker, '
          'passes the marker, and continues east. The next view must keep the cart east of the marker; '
          'it does not reverse or teleport. The marker never moves. No characters speak.')
EXPECTED = (
    'First view: the blue cart starts left of the red marker and finishes right of it. The audience sees the pass.',
    'Next view: the blue cart is still right of the marker and continues right. The audience understands it kept going.',
)
BAD = 'The second view puts the cart left of the marker again: the audience sees an unexplained return behind it.'
CORRECTION = 'The blue cart is already east of the red marker and continues east; it never returns west of the marker.'


def ref(obj):
    return ObjectRef(**{key:obj[key] for key in ('object_id','revision','digest')})


class CraftJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mvgp-craft-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state.sqlite')
        self.auth = AuthService(self.store,'https://craft.example')
        self.media = MediaStore(self.store,self.root/'media')
        # The HF-era test world (tests/data_hf_era_documents.json) in a runtime config; only the video method runs.
        documents={role:hf_era_document(role) for role in ('execution_policy','cut_policy','image_routes','video_routes',
            'hf_image_capability','hf_video_capability','video_timing','nbp_edit_evidence','review_routes')}
        self.config=runtime_config(documents,methods={'mvgp-video-v1':RuntimeConfig.load().require_method('mvgp-video-v1')})
        self.rid=self.config.label
        self.projects=Projects(self.store,self.auth,self.media,label=self.config.label)
        token=self.auth.provision_token('maker','agent',[],3600,allow_create_project=True)
        self.actor=self.auth.authenticate(token)
        project=self.projects.create(self.actor,ProjectCreate(idempotency_key='project',title='Synthetic pass and continuation',branch='original',brief=SOURCE))
        self.pid=project['object_id']
        self.actor=self.auth.authenticate(token)
        self.worker_actor=self.auth.authenticate(self.auth.provision_token('worker','worker',[self.pid],3600))
        self.flow=Workflow(self.store,self.auth,self.config)
        self.gates=Gates(self.store,self.flow,self.media)
        self.context=ContextService(self.store,self.auth,self.flow)
        assets=AssetMethods(self.store,self.media,self.flow,route_profiles=self.config.section('image_routes')['profiles'])
        self.compiler=Compiler(self.store,self.auth,self.flow,self.context,assets,
                               route_profiles=self.config.section('video_routes')['profiles'])
        self.reviews=Reviews(self.store,self.auth,self.flow,self.media)
        self.review_tasks=None  # no AI review
        self.review_calls=[]
        self.review_delivery_sizes=[]
        self.observer_calls=[]
        routes=self.config.section('review_routes')
        self.routes=routes
        self.submissions=Submissions(self.store,self.auth,self.flow,self.gates)
        self.batches=Batches(self.store,self.auth,self.flow,self.submissions,self.reviews)
        self.store.set_budget(self.pid,40,'synthetic_unit')
        self.outputs={}
        self.provider_calls=[]
        @contextmanager
        def download(url, host, ip, timeout):
            yield Download('video/mp4',[self.outputs[urlsplit(url).path]])
        downloader=SafeDownloader({'cdn.example'},resolver=lambda host:['8.8.8.8'],transport=download)
        self.jobs=Jobs(self.store,self.auth,self.submissions,self.media,downloader=downloader)
        provider=FakeProvider({'seedance_2_5'},self.provider_cli)
        self.cuts=Cuts(self.store,self.auth,self.flow,self.media)
        reader=Reader(self.jobs,self.config,transport=self.observer_transport)
        self.worker=Worker(self.jobs,provider,self.worker_actor,[self.pid],cuts=self.cuts,reader=reader)
        self.patches=Patches(self.store,self.auth,self.projects,self.flow)
        self.responses={}

    def draft(self, kind, path, content, deps=()):
        return self.projects.revise(self.actor,self.pid,ArtifactRevision(idempotency_key=path.replace("/",":"),expected_revision=0,
            kind=kind,logical_path=path,content=content,dependencies=[ref(d) for d in deps]))

    def review_http(self, request):
        self.review_calls.append(request)
        payload=json.loads(request.content)
        # Fake verdicts exercise service authority only, not understanding.
        self.assertEqual(payload['model'],self.routes['profiles'][self.routes['role_routes']['standards']]['model'])
        self.assertIn('untrusted',payload['messages'][0]['content'].lower())
        images=[part['image_url']['url'] for message in payload['messages'] if isinstance(message['content'],list)
                for part in message['content'] if part['type']=='image_url']
        wire=len(canonical_json(payload).encode('utf-8'))
        text=wire-sum(len(url.split(',',1)[1]) for url in images)
        limits=self.routes['profiles'][self.routes['role_routes']['standards']]['limits']
        self.assertLessEqual(text,limits['max_text_input_bytes_per_turn'])
        self.assertLessEqual(len(images),limits['max_images_per_turn'])
        self.review_delivery_sizes.append({'text_bytes':text,'wire_bytes':wire,'images':len(images)})
        verdict={'verdict':'pass','summary':'Synthetic standards transport fixture only.',
                 'evidence':[{'resource_id':'request','path':'/params/prompt'}],'findings':[]}
        return httpx.Response(200,json={'id':f'review-{len(self.review_calls)}','model':payload['model'],
            'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(verdict)}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}})

    def observer_transport(self, endpoint, payload, headers, timeout, maximum):
        """Synthetic protocol fixture; actual pixels and source bytes remain real."""
        self.assertEqual(endpoint,ENDPOINT)
        self.assertNotIn('Authorization',headers)
        videos=[part['inlineData'] for part in payload['contents'][0]['parts'] if 'inlineData' in part]
        self.assertEqual(len(videos),1)
        self.assertEqual(videos[0]['mimeType'],'video/mp4')
        self.assertEqual(base64.b64decode(videos[0]['data']),self.expected_observer_bytes)
        self.observer_calls.append(payload)
        observed={'observations':[
            {'input_id':'original','timestamp_seconds':0.2,'modality':'visual',
             'fact':'The blue cart is left of the stationary red roadside marker.','interpretation':None},
            {'input_id':'original','timestamp_seconds':0.2,'modality':'audible',
             'fact':'A steady synthetic tone is audible; no dialogue.','interpretation':None}],
            'answers':[{'question_index':0,'verdict':'contradicted','evidence_indices':[0],
                        'explanation':'The cart is west of the marker, not east.'}],
            'uncertainty':['Synthetic fixture response; no demonstrated model comprehension.']}
        return ReaderResponse(200,json.dumps({'modelVersion':'gemini-3.8-flash',
            'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(observed)}]}}],
            'usageMetadata':{'promptTokensDetails':[{'modality':'VIDEO','tokenCount':100},
                {'modality':'AUDIO','tokenCount':20}],'promptTokenCount':120,'candidatesTokenCount':80}}).encode())

    def observe(self, take, key):
        self.expected_observer_bytes=self.media.read(self.pid,take['object_id'],revision=take['revision'])
        job=self.submissions.observe(self.actor,self.pid,ObserveRequest(idempotency_key='observe-'+key,
            expected_revision=take['revision'],media_id=take['object_id'],reader='video',time_scale=1.0,
            questions=['At 0.2 seconds, is the blue cart east of the red roadside marker?']))
        self.assertEqual(self.worker.run_once()[0]['state'],'succeeded')
        completed=self.store.get_object(self.pid,job['object_id'])
        observation=self.store.get_object(self.pid,completed['body']['result']['object_id'])
        self.assertEqual(observation['author'],'reader_service')
        self.assertEqual(observation['body']['source'],ref(take).model_dump())
        self.assertTrue(observation['body']['consumption']['video_evidence_available'])
        self.assertTrue(observation['body']['consumption']['audio_evidence_available'])
        self.assertEqual(len(observation['body']['frames']),6)
        for frame in observation['body']['frames']:
            media=self.store.get_object(self.pid,frame['media']['object_id'])
            self.assertEqual(media['body']['derivative_of'],ref(take).model_dump())
            self.assertTrue(self.media.read(self.pid,media['object_id']).startswith(b'\x89PNG'))
        return observation

    def provider_cli(self, action, request):
        self.assertEqual(action,'create')
        self.provider_calls.append(request)
        _,path=self.responses[request['params']['prompt']]
        return {'id':f'fixture-{len(self.provider_calls)}','status':'completed','result_url':'https://cdn.example'+path}

    def card(self, number):
        card=json.loads((ROOT/'production/templates/card.json').read_text())
        card['shot']=f'S02-0{number}0A'
        card['The material'].update({'the location and INT/EXT with the asset that covers it':'EXT @loc_lane',
            'the time of day':'day','props and vehicles with tags':['@bluecart'],'the running time in seconds':4,
            'the complexity — simple, medium or complex':'simple',
            'the action in one to three sentences':EXPECTED[number-1]})
        card['Direction'].update({'the goal of the shot in one line':EXPECTED[number-1],
            'The dramaturgy — what changed between the start and the end':EXPECTED[number-1],
            'The blocking relative to the camera':'The red marker is at road center. The blue cart is west of it in view one, east of it in view two. Camera remains south.',
            'end state':'The blue cart is east of the red marker.', 'expected visible performance':EXPECTED[number-1]})
        card['Camera']={'shot size':'Wide','movement':'Locked off','lens':'60 degree field of view','angle':'Eye level'}
        card['ACTING TASK']={}
        card['Edit']['how this shot hooks into the next one']='Preserve the blue cart east of the red marker.'
        card['_production']={'model':'seedance_2_5','aspect_ratio':'16:9','resolution':'1080p',
            **writer_fixture.authored(card,store=self.store,pid=self.pid,actor=self.actor,note=f'Card {number}: {EXPECTED[number-1]}')}
        return card

    def prepare(self, shot, selection, key):
        existing=[o for o in self.store.list_objects(self.pid,kind='method') if o['body']['content']['target']['object_id']==shot['object_id']]
        method=self.projects.select_method(self.actor,self.pid,MethodSelection(idempotency_key='method-'+key,
            expected_revision=existing[0]['revision'] if existing else shot['revision'],target=ref(shot),method_id='mvgp-video-v1',
            rationale='Original synthetic prop-relative-motion probe; references-only animation; qualification is not claimed.'))
        candidate=self.compiler.prepare(self.actor,self.pid,PrepareRequest(idempotency_key='prepare-'+key,
            expected_revision=shot['revision'],target=ref(shot),task='stress',method_selection=ref(method),inputs=[ref(selection)]))
        gate=self.gates.evaluate(self.pid,ref(candidate))
        self.assertTrue(gate['mechanical_pass'],gate['blocking'])
        return candidate

    def batch(self, candidates, key):
        return self.batches.create(self.actor,self.pid,BatchRequest(idempotency_key=key,
            expected_revision=self.store.get_object(self.pid,self.pid)['revision'],candidate_ids=[c['object_id'] for c in candidates]))

    def synthetic_movie(self, name, mode):
        folder=self.root/name
        folder.mkdir()
        for frame in range(96):
            image=Image.new('RGB',(160,96),'white')
            draw=ImageDraw.Draw(image)
            draw.rectangle((77,10,83,80),fill='red')
            x=round(20+110*min(frame/36,1)) if mode=='pass' else 105+round(frame/5) if mode=='continue' else 25
            draw.rectangle((x,55,x+10,65),fill='blue')
            image.save(folder/f'{frame:03d}.png')
        output=folder/'result.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-framerate','24','-i',str(folder/'%03d.png'),
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4',
            '-vf','scale=1920:1080,setsar=1','-c:v','libx264','-threads','1',
            '-pix_fmt','yuv420p','-c:a','aac','-shortest',str(output)],check=True,capture_output=True,timeout=30)
        return output.read_bytes()

    def x_position(self, media, seconds):
        path=self.media.path_for(self.pid,media['object_id'],revision=media['revision'])
        raw=subprocess.run(['ffmpeg','-v','error','-ss',str(seconds),'-i',str(path),'-frames:v','1',
            '-f','image2pipe','-vcodec','png','-'],check=True,capture_output=True,timeout=30).stdout
        image=Image.open(io.BytesIO(raw)).convert('RGB').resize((160,96))
        points=[x for y in range(image.height) for x in range(image.width)
                if (c:=image.getpixel((x,y)))[2]>150 and c[0]<100 and c[1]<100]
        self.assertTrue(points)
        return sum(points)/len(points)/image.width

    def take(self, batch, candidate):
        child=next(c for c in batch['body']['children'] if c['candidate']['object_id']==candidate['object_id'])
        job=self.store.get_object(self.pid,child['job']['object_id'])
        self.assertEqual(job['body']['state'],'succeeded',job['body'])
        return self.store.get_object(self.pid,job['body']['result']['object_id'])
