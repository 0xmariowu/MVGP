"""Exact edit manifests and local rendering; assembly is not film acceptance."""
from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

from production.auth import AuthService, Principal
from production.contracts import (
    CutRequest,
    DomainError,
    ObjectRef,
    content_hash,
    new_id,
)
from production.jobs import Jobs
from production.media import MediaStore
from production.store import Store
from production.workflow import Workflow

# LOCAL bounds, not total process RSS limits. The deployed Linux FFV1 encoder
# requests 306,909,184 bytes for a 1080p worst-case packet, even for simple frames.
# Only lossless intermediate encoding receives the larger allocation allowance.
RENDER_MAX_SINGLE_ALLOCATION_BYTES = 128 * 1024 * 1024
LOSSLESS_MAX_SINGLE_ALLOCATION_BYTES = 512 * 1024 * 1024


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: obj[k] for k in ('object_id', 'revision', 'digest')}


def _unique(values: list[Any]) -> list[Any]:
    return list({content_hash(value): value for value in values}.values())


def _boundaries(segments: list[dict[str, Any]], rate: int) -> list[tuple[int, int]]:
    """Round cumulative boundaries once; per-segment rounding accumulates drift."""
    elapsed, previous, result = Fraction(0), 0, []
    for segment in segments:
        elapsed += Fraction(str(segment['end_seconds'])) - Fraction(str(segment['start_seconds']))
        end = int(elapsed * rate + Fraction(1, 2))
        result.append((previous, end))
        previous = end
    return result


class Cuts:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow, media: MediaStore,
                 *, ffmpeg: Path | None = None) -> None:
        self.store, self.auth, self.workflow, self.media = store, auth, workflow, media
        self.ffmpeg = (ffmpeg or Path(shutil.which('ffmpeg') or '/unavailable/ffmpeg')).resolve()

    def policy(self) -> dict[str, Any]:
        try:
            policy = self.workflow.config.section('cut_policy')
            if (policy['policy_id'] != 'local-cut-assembly-v1' or policy['assembly']['transition'] != 'hard-cut'
                    or policy['assembly']['implicit_trim_seconds'] != 0 or policy['assembly']['playback_rate'] != 1
                    or policy['output']['video_codec'] != 'h264' or policy['output']['audio_codec'] != 'aac'
                    or policy['sound']['mixing'] is not False or policy['sound']['max_sound_inputs'] != 1
                    or policy['assembly']['trim_mode'] != 'exact-source' or policy['output']['container'] != 'mp4'
                    or policy['output']['frame_quantization'] != 'cumulative-nearest-frame'
                    or policy['output']['audio_quantization'] != 'cumulative-nearest-sample'
                    or policy['output']['pixel_format'] != 'yuv420p' or policy['output']['fit'] != 'contain'
                    or policy['sound']['default_mode'] != 'source-audio' or policy['sound']['missing_source_audio'] != 'silence'
                    or policy['sound']['explicit_mode'] != 'whole-cut-replacement' or policy['sound']['replacement_start_seconds'] != 0
                    or policy['sound']['replacement_min_duration'] != 'cut-duration'
                    or policy['context']['mode'] != 'aggregate-source-methods'):
                raise ValueError('Unsupported assembly policy')
            for key in ('width', 'height', 'fps', 'audio_sample_rate', 'audio_channels'):
                if type(policy['output'][key]) is not int or policy['output'][key] <= 0:
                    raise ValueError('Invalid output settings')
            for key in ('max_duration_seconds', 'max_segments', 'max_input_bytes', 'max_total_input_bytes', 'max_intermediate_bytes', 'timeout_seconds'):
                if type(policy['limits'][key]) is not int or policy['limits'][key] <= 0:
                    raise ValueError('Invalid assembly bounds')
            return policy
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError('release_mismatch', 'The runtime config cut policy is incomplete or unsupported') from exc

    def _media(self, pid: str, ref: ObjectRef, db: sqlite3.Connection) -> dict[str, Any]:
        obj = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
        if obj['kind'] != 'media' or ref.digest is None or obj['digest'] != ref.digest:
            raise DomainError('invalid_media', 'Edit input must name exact media bytes and revision')
        self.media.path_for(pid, ref.object_id, revision=ref.revision)
        return obj

    def create(self, actor: Principal, pid: str, request: CutRequest) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor, pid, 'create-cut', conn=db)
            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                project = self.store.get_object(pid, pid, conn=conn)
                rid = project['body']['release_id']
                policy = self.policy()
                previous = self.store.list_objects(pid, kind='cut', conn=conn)
                previous = [o for o in previous if o['author'] == 'cut_service']
                old = previous[0] if previous else None
                guard = old['revision'] if old else project['revision']
                if request.expected_revision != guard:
                    raise DomainError('revision_conflict', 'Assembly changed', current_revision=guard)
                if old:
                    self.workflow.guard_mutation(self.store, pid, old['object_id'], conn)
                if len(request.segments) > policy['limits']['max_segments'] or len(request.sound_inputs) > 1:
                    raise DomainError('invalid_input', 'Edit exceeds released segment or replacement-sound bounds')
                segments, sound, contexts, references, methods, deps = [], [], [], [], [], []
                total, size = 0.0, 0
                imported = False
                for segment in request.segments:
                    media = self._media(pid, segment.take, conn)
                    probe = media['body']['probe']
                    if not probe.get('has_video') or segment.end_seconds > probe['duration']:
                        raise DomainError('invalid_media', 'Trim is outside the actual video')
                    graph = self.workflow.pinned_graph(pid, ObjectRef(**_ref(media)), conn=conn)
                    if graph['stale']:
                        raise DomainError('stale_input', 'Reassemble from current source takes; historical cuts remain available')
                    is_imported = media['author'] == 'importer_service' and media['body'].get('import_status') == 'imported-unverified'
                    if media['author'] != 'worker_service' and not is_imported:
                        raise DomainError('invalid_media', 'Take provenance is neither service generation nor explicit legacy import')
                    if is_imported:
                        imported = True
                        contexts.append({'creative':{'brief':project['body'].get('brief',''), 'scripts':[], 'scenes':[],
                            'shots':[], 'expectations':[], 'source_understanding':[], 'assets':[], 'selections':[]},
                            'context_hash':'', 'project':project, 'branch':project['body']['branch'], 'release_id':rid,
                            'prior_results':[], 'governing_rules':[], 'sources':{},
                            'limitations':['Imported-unverified source; source intent and independent review are missing.']})
                        methods.append({'imported_media':_ref(media), 'method':None, 'status':'imported-unverified'})
                    else:
                        origin = self.workflow.media_origin(pid, ObjectRef(**_ref(media)), conn=conn)
                        if origin['kind'] != 'candidate':
                            raise DomainError('missing_prerequisite', 'Take requires its direct generation intent and method context')
                        producer = origin['authority']
                        candidate = producer['body']
                        self.workflow.config.require_method(candidate['method_id'], task=candidate['task'])
                        contexts.append(candidate['context'])
                        references.extend(candidate['request']['references'])
                        methods.append({'candidate':_ref(producer), 'method':candidate['context']['method'],
                                        'source_release_id':candidate['release_id'],
                                        'method_selection':candidate['method_selection']})
                    duration = segment.end_seconds - segment.start_seconds
                    segments.append({**segment.model_dump(), 'take':_ref(media), 'source_sha256':media['body']['sha256'],
                                     'cut_start_seconds':total, 'duration_seconds':duration,
                                     'audio':'source' if probe.get('has_audio') else 'silence'})
                    total += duration
                    size += media['body']['size']
                    if media['body']['size'] > policy['limits']['max_input_bytes']:
                        raise DomainError('invalid_media', 'Edit input exceeds released size bound')
                    deps.append(_ref(media))
                for ref in request.sound_inputs:
                    media = self._media(pid, ref, conn)
                    if not media['body']['probe'].get('has_audio') or media['body']['probe']['duration'] < total:
                        raise DomainError('invalid_media', 'Replacement sound must cover the entire cut at original speed')
                    if media['body']['size'] > policy['limits']['max_input_bytes']:
                        raise DomainError('invalid_media', 'Replacement sound exceeds released input size bound')
                    size += media['body']['size']
                    sound.append({'media':_ref(media), 'sha256':media['body']['sha256'], 'start_seconds':0.0, 'duration_seconds':total})
                    deps.append(_ref(media))
                if total > policy['limits']['max_duration_seconds'] or size > policy['limits']['max_total_input_bytes']:
                    raise DomainError('invalid_media', 'Assembly exceeds released duration or total byte bounds')
                frame_bounds = _boundaries(segments, policy['output']['fps'])
                if any(end <= start for start, end in frame_bounds):
                    raise DomainError('invalid_input', 'A declared segment has no frame at the released output frame rate')
                sample_bounds = _boundaries(segments, policy['output']['audio_sample_rate'])
                for planned_segment, frames, samples in zip(segments, frame_bounds, sample_bounds, strict=True):
                    planned_segment['output_frame_range'] = list(frames)
                    planned_segment['output_sample_range'] = list(samples)
                context = copy.deepcopy(contexts[0])
                context.pop('context_hash')
                for key in context['creative']:
                    if isinstance(context['creative'][key], list):
                        context['creative'][key] = _unique([v for c in contexts for v in c['creative'][key]])
                context.update(task='cut', target=_ref(project), dependencies=_unique(deps),
                               preparation_inputs=[], source_methods=methods,
                               method={'method_id':policy['policy_id'], 'authority':'LOCAL assembly', 'definition':policy},
                               method_choice=None, prior_results=_unique([v for c in contexts for v in c['prior_results']]),
                               governing_rules=_unique([v for c in contexts for v in c['governing_rules']]),
                               sources={k:v for c in contexts for k,v in c['sources'].items()},
                               neighbors={'shots':[], 'cut_order_known':True, 'segments':segments},
                               limitations=_unique([v for c in contexts for v in c['limitations']]))
                # Each source's creative scope survives assembly; selected media
                # also seeds later feedback. Legacy contexts retain their prior
                # dependency scope rather than inheriting only the first shot.
                context['history_roots'] = _unique([*deps, *[ref for c in contexts
                    for ref in c.get('history_roots', [*c.get('dependencies', []),
                        *(item['object_ref'] for item in c.get('preparation_inputs', []))])]])
                context['context_hash'] = content_hash(context)
                oid = old['object_id'] if old else new_id('cut')
                body = {'lineage_target':oid, 'release_id':rid, 'method_id':policy['policy_id'],
                        'method_selection':None, 'policy_hash':content_hash(policy), 'context':context,
                        'request':{'references':_unique(references)}, 'intent':request.intent,
                        'segments':segments, 'sound_inputs':sound, 'duration_seconds':total,
                        'sound_mode':'whole-cut-replacement' if sound else 'source-audio',
                        'source_methods':methods, 'output':policy['output'], 'label':'rough-cut',
                        'accepted':False, 'imported_unverified':imported, 'dependencies':_unique(deps)}
                if old:
                    return self.store.append_revision(pid, oid, old['revision'], body, 'cut_service', conn=conn)
                return self.store.create_object(pid, 'cut', body, 'cut_service', object_id=oid, conn=conn)
            return self.store.run_idempotent(f'{pid}:{actor.credential_id}:cut', request.idempotency_key,
                                             request.model_dump(), save, conn=db)

    def render(self, worker: Principal, pid: str, cut_ref: ObjectRef, *, jobs: Jobs,
               job_ref: ObjectRef, fence: int) -> dict[str, Any]:
        """Called only for a journaled local render job. IO holds no DB write lock."""
        with self.store.transaction(write=False) as db:
            job = jobs._fenced(worker,pid,job_ref,fence,db)
            intent = self.store.get_object(pid,job['body']['intent']['object_id'],conn=db)
            cut = self.store.get_object(pid,cut_ref.object_id,revision=cut_ref.revision,conn=db)
            body = cut['body']
            if (cut['author'] != 'cut_service' or cut['digest'] != cut_ref.digest or job['body']['state'] != 'dispatching'
                    or intent['body']['operation'] != 'render-cut' or intent['body']['target'] != _ref(cut)):
                raise DomainError('forbidden', 'Render requires its exact fenced local dispatch')
            policy = self.policy()
            if content_hash(policy) != body['policy_hash']:
                raise DomainError('release_mismatch', 'Cut policy changed')
            paths = [self.media.path_for(pid,s['take']['object_id'],revision=s['take']['revision']) for s in body['segments']]
        output = body['output']
        deadline = time.monotonic() + min(policy['limits']['timeout_seconds'], job['body']['lease']['expires_at']-jobs.clock()-1)
        def run(args: list[str], *, lossless: bool = False) -> None:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise DomainError('provider_failure','Local render deadline exceeded')
            try:
                allocation = LOSSLESS_MAX_SINGLE_ALLOCATION_BYTES if lossless else RENDER_MAX_SINGLE_ALLOCATION_BYTES
                subprocess.run([str(self.ffmpeg), '-nostdin', '-v','error','-y','-max_alloc',str(allocation),
                                '-filter_threads','1',*args], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=remaining)
            except (OSError,subprocess.SubprocessError) as exc:
                raise DomainError('provider_failure','Local renderer failed without publishing a result') from exc
        with tempfile.TemporaryDirectory(prefix='mvgp-cut-') as temporary:
            root = Path(temporary)
            # Lossless video and PCM have independent clocks. Quantize cumulative
            # boundaries, never each duration; encode H264/AAC only once at delivery.
            # This retains the declared source trims without cumulative AAC priming
            # or per-segment frame rounding. Sub-frame output precision is impossible.
            frame_bounds = _boundaries(body['segments'], output['fps'])
            sample_bounds = _boundaries(body['segments'], output['audio_sample_rate'])
            if any(end <= start for start, end in frame_bounds):
                raise DomainError('invalid_input', 'A declared segment has no output frame')
            budget = policy['limits']['max_intermediate_bytes']
            def remaining_bytes() -> int:
                remaining = budget - sum(p.stat().st_size for p in root.iterdir() if p.is_file())
                if remaining <= 0:
                    raise DomainError('invalid_media', 'Local render intermediate byte bound exceeded')
                return remaining
            audio = root/'sound.pcm'
            def append_audio(path: Path | None, start: float, duration: float, samples: int, *, pad: bool = True) -> None:
                byte_count = samples * output['audio_channels'] * 2
                if byte_count * 2 >= remaining_bytes():
                    raise DomainError('invalid_media', 'Local audio intermediates exceed byte bound')
                part = root/'part.pcm'
                if path is None:
                    with part.open('wb') as stream:
                        left = byte_count
                        while left:
                            block = min(left, 1024*1024)
                            stream.write(bytes(block))
                            left -= block
                else:
                    padding = f'apad=whole_len={samples},' if pad else ''
                    run(['-protocol_whitelist','file,pipe','-threads','2','-ss',str(start),'-t',str(duration),
                         '-i',str(path),'-map','0:a:0','-vn','-af',
                         f"aresample={output['audio_sample_rate']},{padding}atrim=end_sample={samples},asetpts=PTS-STARTPTS",
                         '-c:a','pcm_s16le','-ar',str(output['audio_sample_rate']),'-ac',str(output['audio_channels']),
                         '-f','s16le','-fs',str(remaining_bytes()),str(part)])
                if part.stat().st_size != byte_count:
                    raise DomainError('invalid_media', 'Decoded source audio does not match the declared sample interval')
                remaining_bytes()
                with audio.open('ab') as destination, part.open('rb') as source:
                    shutil.copyfileobj(source, destination, 1024*1024)
                part.unlink()
                remaining_bytes()
            for index, (segment,path) in enumerate(zip(body['segments'],paths,strict=True)):
                duration = segment['duration_seconds']
                frame_count = frame_bounds[index][1] - frame_bounds[index][0]
                run(['-protocol_whitelist','file,pipe','-threads','2','-ss',str(segment['start_seconds']),
                     '-t',str(duration),'-i',str(path),'-map','0:v:0','-an','-vf',
                     f"setpts=PTS-STARTPTS,scale={output['width']}:{output['height']}:force_original_aspect_ratio=decrease,pad={output['width']}:{output['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={output['fps']},tpad=stop_mode=clone:stop_duration={1/output['fps']}",
                     '-frames:v',str(frame_count),'-c:v','ffv1','-pix_fmt','yuv420p','-threads','2',
                     '-fs',str(remaining_bytes()),str(root/f'{index}.mkv')], lossless=True)
                remaining_bytes()
                if not body['sound_inputs']:
                    append_audio(path if segment['audio']=='source' else None, segment['start_seconds'], duration,
                                 sample_bounds[index][1]-sample_bounds[index][0])
            if body['sound_inputs']:
                ref = body['sound_inputs'][0]['media']
                append_audio(self.media.path_for(pid,ref['object_id'],revision=ref['revision']),
                             0.0,body['duration_seconds'],sample_bounds[-1][1],pad=False)
            (root/'concat.txt').write_text(''.join(f"file '{i}.mkv'\n" for i in range(len(paths))))
            final = root/'final.mp4'
            run(['-protocol_whitelist','file,pipe','-threads','2','-f','concat','-safe','1','-i',str(root/'concat.txt'),
                 '-f','s16le','-ar',str(output['audio_sample_rate']),'-ac',str(output['audio_channels']),'-i',str(audio),
                 '-map','0:v:0','-map','1:a:0','-vf',f"setpts=N/({output['fps']}*TB)",
                 '-r',str(output['fps']),'-c:v','libx264','-preset','fast','-pix_fmt','yuv420p','-threads','2',
                 '-c:a','aac','-ar',str(output['audio_sample_rate']),'-ac',str(output['audio_channels']),
                 '-fs',str(min(self.media.max_bytes, remaining_bytes())),str(final)])
            remaining_bytes()
            try:
                # This is the service's two-stream output, not arbitrary uploaded
                # probe data. Check the exact encoded frame count before acceptance.
                probe = subprocess.run(['ffprobe','-v','error','-select_streams','v:0',
                    '-show_entries','stream=nb_frames,r_frame_rate,codec_name,pix_fmt','-of','json',str(final)],
                    stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=max(0.001,deadline-time.monotonic()))
                video = json.loads(probe.stdout)['streams'][0]
                if (int(video['nb_frames']) != frame_bounds[-1][1]
                        or Fraction(video['r_frame_rate']) != output['fps']
                        or video['codec_name'] != 'h264' or video['pix_fmt'] != output['pixel_format']):
                    raise ValueError('Encoded video differs from the global frame timeline')
            except (OSError,subprocess.SubprocessError,ValueError,KeyError,IndexError) as exc:
                raise DomainError('invalid_media','Rendered frames do not match the exact global timeline') from exc
            def publish(metadata: dict[str, Any]) -> dict[str, Any]:
                probe = metadata['probe']
                if (not probe.get('has_video') or not probe.get('has_audio') or probe['width']!=output['width']
                        or probe['height']!=output['height'] or abs(probe['duration']-body['duration_seconds']) > 2/output['fps']):
                    raise DomainError('invalid_media','Rendered output does not match the exact edit contract')
                with self.store.transaction() as db:
                    current = jobs._fenced(worker,pid,job_ref,fence,db)
                    graph = self.workflow.pinned_graph(pid,cut_ref,conn=db)
                    media = self.store.create_object(pid,'media',{**metadata,'dependencies':[_ref(cut)],
                        'source_cut':_ref(cut),'assembly_manifest':_ref(cut),'label':'rough-cut',
                        'current':not graph['stale'],'accepted':False},'cut_service',conn=db)
                    jobs._write(pid,current,{'state':'succeeded','result':_ref(media),'lease':None,
                        'current':not graph['stale'],'non_current_reasons':graph['reasons']},'cut.rendered',db)
                    return media
            with final.open('rb') as stream:
                return self.media.put(pid,iter(lambda:stream.read(1024*1024),b''),'video/mp4','cut_service',publish=publish)
