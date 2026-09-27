"""Pinned Higgsfield CLI boundary with explicit service-only live enablement.

Source: CLI 1.1.23 model get for seedance_2_5, nano_banana_pro and gpt_image_2_5.
Read-only get returns id/job_type/status/result_url/min_result_url/created_at/params.
Native 1.1.23 create emits a one-element UUID array (verified in an offline Linux
container). It grants submission identity only; get supplies status and results.
Errors are never resubmitted here; unknown outcomes retain their reservation.
Explicit service enablement does not replace release, review or budget gates.

Pin the native @higgsfield/cli/vendor/hf executable, not the Node shim or a PATH
lookup for the unrelated HuggingFace hf command. OAuth belongs to the isolated
service HOME; no caller environment, shell, proxy or agent credential is inherited.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, overload
from urllib.parse import urlsplit, urlunsplit

from production import listing_reconcile, review_http
from production.contracts import DomainError, content_hash, new_id
from production.provider_types import ProviderReceipt, ResolvedReference

ALLOWED_MODELS = frozenset({'nano_banana_pro', 'seedance_2_5', 'gpt_image_2_5'})
SCALAR_PARAMS = {'nano_banana_pro':frozenset({'prompt','resolution','aspect_ratio'}),
                'gpt_image_2_5':frozenset({'prompt','resolution','aspect_ratio','quality','variant'}),
                'seedance_2_5':frozenset({'prompt','resolution','aspect_ratio','duration','mode','generate_audio','bitrate_mode'})}
NATIVE_HEADERS = (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf')
REFERENCE_SUFFIXES = {'image/png':'.png', 'image/jpeg':'.jpg', 'image/webp':'.webp',
                      'video/mp4':'.mp4', 'video/quicktime':'.mov',
                      'audio/wav':'.wav', 'audio/mpeg':'.mp3', 'audio/mp4':'.m4a'}


UPLOAD_JPEG_ABOVE = 1_000_000


def _upload_copy(path: Path) -> Path:
    """Return a smaller JPEG sibling for a large opaque PNG/WebP still, else the path itself."""
    if path.suffix not in ('.png', '.webp') or path.stat().st_size <= UPLOAD_JPEG_ABOVE:
        return path
    from PIL import Image
    with Image.open(path) as image:
        image.load()
        if 'A' in image.getbands() and image.getchannel('A').getextrema()[0] < 255:
            return path
        target = path.with_suffix('.jpg')
        image.convert('RGB').save(target, 'JPEG', quality=92)
    return target


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: bytes = field(repr=False)
    stderr: bytes = field(default=b'', repr=False)
    termination: str = 'exited'
    truncated: bool = False
    cleanup_failed: bool = False


class NativeTimeout(TimeoutError):
    def __init__(self, result: CommandResult) -> None:
        super().__init__('Bounded CLI deadline exceeded')
        self.result = result


class NativeOutputLimit(ValueError):
    def __init__(self, result: CommandResult) -> None:
        super().__init__('Bounded CLI output exceeded')
        self.result = result


EvidenceSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ExecutablePin:
    path: Path
    sha256: str
    version: str


Transport = Callable[[list[str], dict[str, str], float, int], CommandResult]


def _native(argv: list[str], env: dict[str, str], timeout: float, max_bytes: int) -> CommandResult:
    """Bound both pipes while running; no retries, shell or unbounded communicate."""
    review_http.assert_healthy()
    process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    selector: selectors.BaseSelector | None = None
    assert process.stdout is not None and process.stderr is not None
    streams = (process.stdout, process.stderr)
    buffers = {stream.fileno(): bytearray() for stream in streams}
    started = time.monotonic()
    completed = False
    failed: CommandResult | None = None
    def snapshot(termination: str, truncated: bool = False) -> CommandResult:
        return CommandResult(None, bytes(buffers[streams[0].fileno()]),
                             bytes(buffers[streams[1].fileno()]), termination, truncated)
    try:
        selector = selectors.DefaultSelector()
        for stream in streams:
            os.set_blocking(stream.fileno(),False)
            selector.register(stream,selectors.EVENT_READ)
        while selector.get_map():
            remaining = timeout-(time.monotonic()-started)
            if remaining <= 0:
                raise NativeTimeout(snapshot('timeout'))
            for key,_ in selector.select(min(remaining,0.1)):
                chunk = os.read(key.fd,65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = max_bytes - sum(len(value) for value in buffers.values())
                buffers[key.fd].extend(chunk[:available])
                if len(chunk) > available:
                    raise NativeOutputLimit(snapshot('output_limit', True))
        code = process.wait(timeout=max(0.001,timeout-(time.monotonic()-started)))
        completed = True
        return CommandResult(code,bytes(buffers[process.stdout.fileno()]),bytes(buffers[process.stderr.fileno()]))
    except (NativeTimeout, NativeOutputLimit) as exc:
        failed = exc.result
        raise
    except subprocess.TimeoutExpired:
        failed = snapshot('timeout')
        raise NativeTimeout(failed) from None
    except OSError as exc:
        # Preserve bytes already read, without placing argv/environment in the exception.
        failed = snapshot('io_error')
        exc.command_result = failed  # type: ignore[attr-defined]
        raise
    finally:
        try:
            if not completed:
                try:
                    review_http.terminate_owned_process(process)
                except review_http.FatalWorkerError as fatal:
                    # Carry bounded bytes out through the fatal path. Raw evidence
                    # remains private and cannot grant retry or result authority.
                    fatal.command_result = replace(failed or snapshot('io_error'), cleanup_failed=True)  # type: ignore[attr-defined]
                    raise
        finally:
            try:
                if selector is not None:
                    selector.close()
            except (OSError, ValueError):
                pass
            finally:
                for stream in streams:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass


def _redact(value: Any) -> Any:
    if isinstance(value,dict):
        return {str(k): '[redacted]' if any(word in str(k).lower() for word in ('token','secret','password','authorization','cookie','api_key'))
                else _redact(v) for k,v in value.items()}
    if isinstance(value,list):
        return [_redact(v) for v in value]
    if isinstance(value,str):
        def sanitize(match: re.Match[str]) -> str:
            try:
                parsed=urlsplit(match[0])
                return urlunsplit((parsed.scheme,parsed.hostname or '',parsed.path,'',''))
            except ValueError:
                return '[invalid URL]'
        value = re.sub(r'https?://[^\s<>"\']+',sanitize,value)
        value = re.sub(r'(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+',r'\1[redacted]',value)
        value = re.sub(r'(?i)\b(token|secret|password|api_key)\s*[:=]\s*[^\s,;]+',r'\1=[redacted]',value)
    return value


class HFProvider:
    def __init__(self, capabilities: Mapping[str, dict[str, Any]], executable: ExecutablePin, *,
                 service_home: Path, service_uid: int, media_root: Path, transport: Transport | None = None,
                 timeout: float = 60, max_output_bytes: int = 1048576,
                 live_enabled: bool = False, credits_per_second: Mapping[str, Mapping[str, int]] | None = None) -> None:
        """Trusted composition only; neither transport nor executable is author input."""
        if (not capabilities or not set(capabilities) <= ALLOWED_MODELS or not 0 < timeout <= 120
                or type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 4194304):
            raise ValueError('Explicit supported capabilities and bounded CLI limits are required')
        if type(live_enabled) is not bool or live_enabled and transport is not None:
            raise ValueError('Live enablement cannot be combined with an injected transport')
        self.live_enabled = live_enabled
        self._capabilities = json.loads(json.dumps(dict(capabilities)))
        if any(doc.get('job_type') != name for name,doc in self._capabilities.items()):
            raise ValueError('Capability identity differs from its route')
        self.pin, self.home, self.uid = executable, service_home, service_uid
        self.root = media_root.resolve(strict=True)
        self.fake, self.transport = transport is not None, transport or _native
        self.timeout, self.max_bytes, self._version_checked = timeout, max_output_bytes, False
        # a finished take settles at the pinned credits per second x its seconds
        # (`higgsfield generate cost`, 2026-09-27); without a pinned price the whole hold stays as unknown cost.
        self.credits_per_second = {job: dict(rates) for job, rates in (credits_per_second or {}).items()}

    def _isolation(self) -> dict[str, str]:
        path = self.pin.path
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 67108864 or path.stat().st_mode & 0o022):
            raise DomainError('forbidden','Provider executable differs from pinned native binary')
        with path.open('rb') as stream:
            binary = stream.read(67108865)
        if (len(binary) > 67108864 or hashlib.sha256(binary).hexdigest() != self.pin.sha256
                or binary[:4] not in NATIVE_HEADERS):
            raise DomainError('forbidden','Provider executable differs from pinned native binary')
        if (not self.home.is_absolute() or self.home.is_symlink() or not self.home.is_dir()
                or self.home.stat().st_uid != self.uid or self.home.stat().st_mode & 0o077
                or os.geteuid() != self.uid):
            raise DomainError('forbidden','Provider credentials require the isolated service identity and private HOME')
        return {'HOME':str(self.home),'PATH':'/usr/bin:/bin','NO_COLOR':'1'}

    def _evidence(self, result: CommandResult, args: list[str], invocation_id: str) -> dict[str, Any]:
        stdout = result.stdout[:self.max_bytes]
        stderr = result.stderr[:self.max_bytes-len(stdout)]
        clipped = len(result.stdout)+len(result.stderr) > self.max_bytes
        def stream(data: bytes) -> dict[str, Any]:
            return {'base64':base64.b64encode(data).decode('ascii'), 'size':len(data),
                    'sha256':hashlib.sha256(data).hexdigest()}
        return {'stage':'native-command', 'schema_version':1, 'invocation_id':invocation_id,
                'operation':args[1], 'expected_job_id':args[2] if args[1]=='get' else None,
                'executable':{'sha256':self.pin.sha256, 'version':self.pin.version},
                'returncode':result.returncode,
                'termination':'output_limit' if clipped else result.termination,
                'truncated':result.truncated or clipped, 'cleanup_failed':result.cleanup_failed,
                'stdout':stream(stdout), 'stderr':stream(stderr)}

    @overload
    def _call(self, args: list[str], *, submission: Literal[False] = False,
              evidence_sink: EvidenceSink | None = None, listing: Literal[False] = False) -> dict[str, Any]: ...

    @overload
    def _call(self, args: list[str], *, submission: Literal[False] = False,
              evidence_sink: EvidenceSink | None = None, listing: Literal[True]) -> dict[str, Any] | list[Any]: ...

    @overload
    def _call(self, args: list[str], *, submission: Literal[True],
              evidence_sink: EvidenceSink | None = None) -> dict[str, Any] | list[Any]: ...

    def _call(self, args: list[str], *, submission: bool = False,
              evidence_sink: EvidenceSink | None = None, listing: bool = False) -> dict[str, Any] | list[Any]:
        review_http.assert_healthy()
        env = self._isolation()
        try:
            if not self._version_checked:
                version = self.transport([str(self.pin.path),'--version'],env,self.timeout,self.max_bytes)
                if (version.returncode or len(version.stdout)+len(version.stderr) > self.max_bytes
                        or version.stdout.decode().strip() != self.pin.version):
                    raise DomainError('forbidden','Provider version differs from the executable pin')
                self._version_checked = True
            invocation_id = new_id('command')
            try:
                result = self.transport([str(self.pin.path),*args,'--json'],env,self.timeout,self.max_bytes)
            except review_http.FatalWorkerError as fatal:
                try:
                    partial = getattr(fatal, 'command_result', None)
                    if evidence_sink is not None and isinstance(partial, CommandResult):
                        evidence_sink(self._evidence(partial,args,invocation_id))
                finally:
                    # Journal failure must never turn an unreaped child into an
                    # ordinary job exception that the worker could continue past.
                    raise fatal from None
            except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
                partial = getattr(exc, 'result', getattr(exc, 'command_result', None))
                if isinstance(partial, CommandResult):
                    result = partial
                else:
                    partial_out = exc.stdout if isinstance(exc, subprocess.TimeoutExpired) else b''
                    partial_err = exc.stderr if isinstance(exc, subprocess.TimeoutExpired) else b''
                    result = CommandResult(None, partial_out if isinstance(partial_out, bytes) else b'',
                        partial_err if isinstance(partial_err, bytes) else b'',
                        'timeout' if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else 'io_error')
                if evidence_sink is not None:
                    evidence_sink(self._evidence(result,args,invocation_id))
                raise
            if evidence_sink is not None:
                evidence_sink(self._evidence(result,args,invocation_id))
            if result.termination != 'exited' or result.returncode != 0 or len(result.stdout)+len(result.stderr) > self.max_bytes:
                raise ValueError('CLI failed or exceeded output bound')
            parsed = json.loads(result.stdout)
            # `generate list --json` prints a bare array (observed 2026-09-24).
            if not isinstance(parsed,dict) and not ((submission or listing) and isinstance(parsed,list)):
                raise TypeError('Expected one bounded JSON object')
            return parsed
        except DomainError:
            raise
        except (OSError, ValueError, TypeError, TimeoutError, subprocess.TimeoutExpired, UnicodeError):
            raise DomainError('unknown_outcome' if submission else 'provider_failure',
                              'Provider response unavailable or invalid; no automatic retry') from None

    def capabilities(self, job_type: str) -> dict[str, Any]:
        if job_type not in self._capabilities:
            raise DomainError('unsupported_route','Model was not pinned by this service')
        current = self._call(['model','get',job_type])
        if content_hash(current) != content_hash(self._capabilities[job_type]):
            raise DomainError('release_mismatch','Provider capability changed; publish and verify a new route')
        return current

    def _parameters(self, request: dict[str, Any], *, has_references: bool) -> list[str]:
        if set(request) != {'job_type','params','references'}:
            raise DomainError('unsupported_route','Only the compiled request schema is accepted')
        model,params = request['job_type'],request['params']
        if model not in self._capabilities or not isinstance(params,dict) or not set(params) <= SCALAR_PARAMS[model]:
            raise DomainError('unsupported_route','Unsupported model or raw provider parameter')
        definitions = {p['name']:p for p in self._capabilities[model]['params']}
        args=[]
        for name,value in params.items():
            spec=definitions.get(name)
            if spec is None or not isinstance(value,(str,int,bool)):
                raise DomainError('unsupported_route','Unverified provider parameter type')
            expected={'string':str,'integer':int,'boolean':bool}.get(spec['type'])
            if (expected is None or type(value) is not expected or 'enum' in spec and value not in spec['enum']
                    or isinstance(value,str) and ('\x00' in value or len(value.encode())>65536)
                    or name=='duration' and (not isinstance(value,int) or value<=0 or value>2**31-1)):
                raise DomainError('invalid_input','Parameter is outside pinned capability',field=name)
            args += ['--'+name.replace('_','-'),str(value).lower() if isinstance(value,bool) else str(value)]
        required={p['name'] for p in definitions.values() if p.get('required')}
        if not required <= params.keys() or not {'resolution','aspect_ratio'} <= params.keys():
            raise DomainError('invalid_input','Critical output settings must be explicit')
        if model=='gpt_image_2_5' and not {'quality','variant'} <= params.keys():
            raise DomainError('invalid_input','GPT Image quality and variant must be explicitly released')
        if model=='seedance_2_5':
            if not {'mode','duration'} <= params.keys() or params['mode'] not in ('t2v','omni_reference'):
                raise DomainError('unsupported_route','Only adopted t2v/omni_reference generation is mapped')
            if (params['mode']=='t2v') == has_references:
                raise DomainError('invalid_input','Reference count contradicts selected generation mode')
        if not params.get('prompt') and not has_references:
            raise DomainError('invalid_input','Empty generation input')
        return args

    def _references(self, request: dict[str, Any], resolved: list[ResolvedReference]) -> list[str]:
        declared=request['references']
        if not isinstance(declared,list) or len(declared)!=len(resolved):
            raise DomainError('invalid_media','Resolved media must exactly match frozen reference order')
        definitions={p['name'] for p in self._capabilities[request['job_type']]['params']}
        args=[]
        counts={'image':0,'video':0,'audio':0}
        for metadata,ref in zip(declared,resolved,strict=True):
            if (metadata.get('object_ref')!=ref.object_ref or metadata.get('sha256')!=ref.sha256
                    or metadata.get('media_type')!=ref.media_type):
                raise DomainError('invalid_media','Resolved media identity differs from frozen input')
            if ref.media_type not in REFERENCE_SUFFIXES:
                raise DomainError('unsupported_route','Reference MIME type has no supported native upload suffix')
            modality=ref.media_type.split('/')[0]
            name=modality+'_references'
            if modality not in counts or name not in definitions:
                raise DomainError('unsupported_route','Reference modality is not supported by this model')
            path=ref.path
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(self.root) or not stat.S_ISREG(path.stat().st_mode)):
                raise DomainError('invalid_media','Reference is outside verified service media storage')
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1048576),b''):
                    digest.update(chunk)
            if digest.hexdigest()!=ref.sha256:
                raise DomainError('invalid_media','Reference bytes changed')
            counts[modality]+=1
            args += ['--'+name.replace('_','-'),str(path)]
        if (request['job_type']=='gpt_image_2_5' and counts['image']>3
                or request['job_type']=='nano_banana_pro' and sum(counts.values())>14
                or sum(counts.values())>50 or counts['image']>30):
            raise DomainError('invalid_input','Reference count exceeds adopted route or pinned model capability')
        return args

    def list_recent(self, job_type: str) -> list[dict[str, Any]]:
        """Read-only page of the workspace's 50 newest jobs of this model's kind; never uploads or creates.

        Settles a timed-out create; prompts leave only as hashes.
        """
        if job_type not in self._capabilities:
            raise DomainError('unsupported_route','Model was not pinned by this service')
        kind=self._capabilities[job_type].get('type')
        if kind not in ('video','image'):
            raise DomainError('unsupported_route','Only video or image models have a job listing')
        return listing_reconcile.listed_jobs(self._call(['generate','list','--'+kind,'--size','50'],listing=True))

    def cost(self, request: dict[str, Any]) -> dict[str, Any]:
        """Read-only estimate without media flags; local refs would auto-upload.

        Schema/unit of CLI estimates is unverified, so raw bounded output is kept
        as non-authoritative evidence; never used to settle or authorize spending.
        """
        if request.get('references'):
            raise DomainError('unsupported_route','Read-only cost refuses references that could trigger upload')
        args=self._parameters(request,has_references=False)
        raw=self._call(['generate','cost',request['job_type'],*args])
        return {'authoritative':False,'settled_cost':None,'raw_receipt':_redact(raw)}

    def submit(self, intent: dict[str, Any], resolved_references: list[ResolvedReference], *,
               evidence_sink: EvidenceSink | None = None) -> ProviderReceipt:
        """Consume dispatch-intent BODY, only after worker commits dispatching.

        evidence_sink is a per-call service-only private writer, not an intent field.
        It runs before interpretation and its failure prevents publication.
        The live flag is service configuration, never part of an author request.
        A native ID array is only an acknowledgement, never a finished result.
        """
        if not self.fake and not self.live_enabled:
            raise DomainError('unsupported_route','Live Higgsfield create requires explicit service enablement; no request sent')
        if intent.get('operation')!='submit' or intent.get('cost',{}).get('mode')!=('fake' if self.fake else 'live'):
            raise DomainError('forbidden','Adapter mode differs from service-authorized intent')
        request=intent['request']
        args=self._parameters(request,has_references=bool(request.get('references')))
        self._references(request,resolved_references)
        self._isolation()
        # Storage identities deliberately have no extension. Native uploads
        # infer media type from filenames, so use owned temporary hard links;
        # never rename canonical blobs or trust an author's logical filename.
        with tempfile.TemporaryDirectory(prefix='hf-references-',dir=self.root) as directory:
            staged=[]
            for number, ref in enumerate(resolved_references):
                path=Path(directory)/(str(number)+REFERENCE_SUFFIXES[ref.media_type])
                os.link(ref.path,path,follow_symlinks=False)
                staged.append(replace(ref,path=path))
            refs=self._references(request,staged)
            # The native upload goes straight to overseas object storage and the CLI
            # abandons any single upload after 60 s; on a slow evening route a 3-4 MB
            # PNG never finishes. Send large opaque stills as JPEG q92 (same pixels
            # to the eye, about a tenth of the bytes); the verified original stays
            # the recorded reference identity.
            refs=[str(_upload_copy(Path(a))) if a.startswith(directory) else a for a in refs]
            raw=self._call(['generate','create',request['job_type'],*args,*refs],submission=True,evidence_sink=evidence_sink)
        if isinstance(raw,list):
            if (len(raw)!=1 or not isinstance(raw[0],str)
                    or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',raw[0])):
                raise DomainError('unknown_outcome','Create returned ambiguous or malformed task IDs; no automatic retry')
            return ProviderReceipt(raw[0],request['job_type'],'accepted','submitted',
                                   {'created_job_ids':raw},{},{})
        return self._receipt(raw,request,submission=True)

    def get(self, job_id: str, expected_request: dict[str, Any], *,
            evidence_sink: EvidenceSink | None = None) -> ProviderReceipt:
        if not isinstance(job_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',job_id):
            raise DomainError('invalid_input','Invalid provider job identity')
        self._parameters(expected_request,has_references=bool(expected_request.get('references')))
        raw=self._call(['generate','get',job_id],evidence_sink=evidence_sink)
        receipt=self._receipt(raw,expected_request)
        if receipt.job_id!=job_id:
            raise DomainError('provider_failure','Provider response belongs to another job')
        return receipt

    def _receipt(self, raw: dict[str, Any], expected: dict[str, Any], *, submission: bool = False) -> ProviderReceipt:
        try:
            job_id,model,status,params=raw['id'],raw['job_type'],raw['status'],raw['params']
            if (not isinstance(job_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',job_id)
                    or model!=expected['job_type'] or not isinstance(status,str) or not isinstance(params,dict)):
                raise ValueError('Malformed provider identity')
            adjustments={k:{'requested':expected['params'].get(k),'reported':v} for k,v in params.items()
                         if k not in expected['params'] or expected['params'][k]!=v or type(expected['params'][k]) is not type(v)}
            # An echo is supplier reporting, not proof of the rendered content.
            # Omitted/defaulted fields cannot establish a conflicting adjustment.
            definitions = {p['name']:p for p in self._capabilities[model]['params']}
            critical, reports = {}, {}
            for name, requested in expected['params'].items():
                report: dict[str, Any] = {'requested':requested,'status':'not_reported'}
                if name in params:
                    reported = params[name]
                    spec = definitions.get(name, {})
                    kind = {'string':str,'integer':int,'boolean':bool}.get(spec.get('type'))
                    valid = (kind is not None and type(reported) is kind
                             and ('enum' not in spec or reported in spec['enum'])
                             and not (isinstance(reported,str) and ('\x00' in reported or len(reported.encode())>65536))
                             and not (name=='duration' and (type(reported) is not int or not 0<reported<=2**31-1)))
                    report.update(reported=reported,status='unverifiable' if not valid else
                                  'matched' if reported==requested else 'conflicting')
                    if valid and reported != requested:
                        critical[name] = {'requested':requested,'reported':reported}
                reports[name] = report
            # 'queued': Higgsfield's first status for an accepted job (live, 2026-09-27); as
            # 'unknown' it would count against the desk's patience for lost takes while the job only waited its turn.
            state={'completed':'succeeded','ip_detected':'failed','in_progress':'running','queued':'running'}.get(status,'unknown')
            url=raw.get('result_url')
            if url is not None:
                parsed=urlsplit(url)
                if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Invalid result URL')
            if state=='succeeded' and not url:
                state='unknown'
            if critical and state=='succeeded':
                state='failed'
            rate=self.credits_per_second.get(model,{}).get(expected['params'].get('resolution'))
            seconds=expected['params'].get('duration')
            settled=(rate*seconds if state=='succeeded' and type(rate) is int and rate>0
                     and type(seconds) is int and seconds>0 else None)
            if status=='ip_detected':
                # Higgsfield refunds a take its IP check stops (live 2026-09-27, -48 at the create,
                # +48 three minutes later on `account transactions`), so it settles at 0 instead of holding the 30 s price.
                settled=0
            return ProviderReceipt(job_id,model,status,state,_redact(raw),_redact(adjustments),_redact(critical),
                                   url if state=='succeeded' else None,parameter_reports=_redact(reports),
                                   settled_cost=settled)
        except (KeyError,ValueError,TypeError,AttributeError) as exc:
            raise DomainError('unknown_outcome' if submission else 'provider_failure','Unverified or malformed provider receipt; no automatic retry') from exc

    @staticmethod
    def result(receipt: ProviderReceipt) -> str:
        """Worker-only URL; download validation belongs to bounded result transport."""
        if receipt.state!='succeeded' or not receipt.result_url or receipt.critical_adjustments:
            raise DomainError('provider_failure','No usable verified provider result')
        return receipt.result_url
