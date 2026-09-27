"""A single private HTTP POST with a parent-enforced process lifetime.

The request budget includes startup, stdin, DNS, upload, headers and body. Failed
requests are killed/reaped before UnknownOutcome, with at most two extra seconds
for cleanup. This cancels local IO, not already accepted upstream inference.
There is no retry. The caller owns durable intent, budgets and result decoding.

The fixed ``-I`` child needs only stdlib and the installed httpx distribution;
it intentionally imports no project module or user/site startup configuration.
"""
from __future__ import annotations

import json
import math
import os
import selectors
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import httpx

ENDPOINTS = frozenset({'https://api.deepseek.com/chat/completions',
                       'https://api.typesafe.ai/v1/systemone',
                       'https://api.apilio.ai/v1/chat/completions',
                       'https://api.apilio.ai/v1beta/models/gemini-3.8-flash:generateContent'})
# LOCAL complete-material capacity, shared by parent, isolated child and
# review consumers. A released profile may select a smaller wire envelope.
MAX_REQUEST_BYTES = 128 * 1024 * 1024
MAX_RESPONSE_BYTES = 4_194_304
MAX_HEADER_BYTES = 16_384
CLEANUP_SECONDS = 2.0
_POISONED = False
FAILURE_REASONS = frozenset({'unavailable', 'spawn_error', 'wall_deadline', 'stdout_limit',
    'stderr_limit', 'child_exit', 'local_io', 'invalid_frame', 'response_encoding',
    'response_limit', 'connect_timeout', 'read_timeout', 'write_timeout', 'pool_timeout',
    'connect_error', 'http_transport'})


class UnknownOutcome(Exception):
    """Local HTTP result is unavailable; hold cost and never retry automatically."""
    def __init__(self, reason: str = 'unavailable') -> None:
        self.reason = reason if reason in FAILURE_REASONS else 'unavailable'
        super().__init__('Review request outcome is unknown; retain reservation and do not retry')


class FatalWorkerError(BaseException):
    """Do not catch as a normal job failure: the hosting worker must stop."""
    def __init__(self) -> None:
        super().__init__('Owned model process termination could not be confirmed; worker must stop')


def is_healthy() -> bool:
    """Readiness hook; the hosting worker must stop after a fatal cleanup failure."""
    return not _POISONED


def assert_healthy() -> None:
    if not is_healthy():
        raise FatalWorkerError()


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: bytes


def _json(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate private frame key')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise TypeError('Private frame must be an object')
    return value


def _frame(header: dict[str, Any], body: bytes) -> bytes:
    data = json.dumps(header, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(data) > MAX_HEADER_BYTES:
        raise ValueError('Private request header exceeds its bound')
    return struct.pack('!I', len(data)) + data + body


def _validate(endpoint: str, payload: bytes, headers: Mapping[str, str], timeout: float, maximum: int) -> None:
    if (not isinstance(endpoint, str) or endpoint not in ENDPOINTS or not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_REQUEST_BYTES
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120
            or type(maximum) is not int or not 0 < maximum <= MAX_RESPONSE_BYTES
            or not isinstance(headers, Mapping) or set(headers) != {'Content-Type', 'Accept-Encoding', 'Authorization'}
            or headers['Content-Type'] != 'application/json' or headers['Accept-Encoding'] != 'identity'
            or not isinstance(headers['Authorization'], str) or not headers['Authorization'].startswith('Bearer ')
            or not 7 < len(headers['Authorization']) <= 8192
            or any(not 32 <= ord(c) <= 126 for c in headers['Authorization'])):
        raise ValueError('Invalid private review HTTP request')


def terminate_owned_process(process: subprocess.Popen[bytes]) -> None:
    """Kill and reap a service-owned session within the cleanup allowance.

    The caller must have started this Popen with start_new_session=True and must
    not poll/reap the leader first: retaining its PID avoids signalling a reused
    group. This service-only seam accepts no author PID or executable. An
    unconfirmed exit poisons all model IO through the shared worker health flag.
    """
    global _POISONED
    deadline = time.monotonic() + CLEANUP_SECONDS
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        # Still confirm exit; an already dead child is safe, a live one is fatal.
        pass
    try:
        process.wait(timeout=max(0.001, deadline-time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        _POISONED = True
        raise FatalWorkerError() from None


# Retain the original private test seam while native adapters adopt the public API.
_reap = terminate_owned_process


def _pump(argv: Sequence[str], data: bytes, timeout: float, maximum: int) -> bytes:
    """Private test seam only; request() always supplies the fixed isolated child.

    stderr is bounded and discarded. It cannot leak request credentials through
    exceptions. Stdin is pumped concurrently, so a child that stops reading cannot
    block outside the same wall deadline.
    """
    assert_healthy()
    started = time.monotonic()
    try:
        process = subprocess.Popen(list(argv), env={}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    except OSError:
        raise UnknownOutcome('spawn_error') from None
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    streams = (process.stdin, process.stdout, process.stderr)
    selector: selectors.BaseSelector | None = None
    output = bytearray()
    errors = 0
    sent = 0
    reaped = False
    try:
        selector = selectors.DefaultSelector()
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        if data:
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            process.stdin.close()
        while selector.get_map():
            remaining = timeout - (time.monotonic()-started)
            if remaining <= 0:
                raise UnknownOutcome('wall_deadline')
            for key, _ in selector.select(min(remaining, 0.05)):
                if key.data == 'stdin':
                    sent += os.write(key.fd, memoryview(data)[sent:sent+65536])
                    if sent == len(data):
                        selector.unregister(key.fd)
                        process.stdin.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fd)
                elif key.data == 'stdout':
                    if len(output)+len(chunk) > maximum:
                        raise UnknownOutcome('stdout_limit')
                    output.extend(chunk)
                else:
                    errors += len(chunk)
                    if errors > MAX_HEADER_BYTES:
                        raise UnknownOutcome('stderr_limit')
        remaining = timeout-(time.monotonic()-started)
        if remaining <= 0:
            raise UnknownOutcome('wall_deadline')
        code = process.wait(timeout=remaining)
        reaped = True
        if code != 0:
            raise UnknownOutcome('child_exit')
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise UnknownOutcome('wall_deadline') from None
    except (OSError, ValueError):
        raise UnknownOutcome('local_io') from None
    finally:
        try:
            if not reaped:
                terminate_owned_process(process)
        finally:
            # Cleanup failures must not hide fatal child termination failure.
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


def request(endpoint: str, payload: bytes, headers: Mapping[str, str], timeout: float, maximum: int) -> HTTPResponse:
    """Service-only fixed endpoint POST; credentials travel only in private stdin.

    Invalid local parameters raise ValueError before a child is launched. Every
    child/response failure raises UnknownOutcome; FatalWorkerError is never an
    ordinary retryable failure. Fake transports are injected outside this helper.
    """
    _validate(endpoint, payload, headers, timeout, maximum)
    header = {'endpoint':endpoint, 'headers':dict(headers), 'timeout':timeout, 'maximum':maximum, 'size':len(payload)}
    raw = _pump([sys.executable, '-I', str(Path(__file__).resolve(strict=True)), '--child'],
                _frame(header, payload), timeout, maximum+MAX_HEADER_BYTES+4)
    try:
        size = struct.unpack('!I', raw[:4])[0]
        if not 0 < size <= MAX_HEADER_BYTES or len(raw) < 4+size:
            raise ValueError('Invalid response frame')
        result = _json(raw[4:4+size])
        body = raw[4+size:]
        if set(result) == {'error'} and isinstance(result['error'], str) and result['error'] in FAILURE_REASONS and not body:
            raise UnknownOutcome(result['error'])
        if (set(result) != {'status_code', 'size'} or type(result['status_code']) is not int
                or not 100 <= result['status_code'] <= 599 or type(result['size']) is not int
                or result['size'] != len(body) or len(body) > maximum):
            raise ValueError('Invalid response frame')
        return HTTPResponse(result['status_code'], body)
    except (ValueError, TypeError, KeyError, struct.error, UnicodeError, RecursionError):
        raise UnknownOutcome('invalid_frame') from None


def _post(endpoint: str, payload: bytes, headers: Mapping[str, str], timeout: float, maximum: int) -> HTTPResponse:
    """Child's one synchronous POST; its parent enforces the actual wall limit."""
    _validate(endpoint, payload, headers, timeout, maximum)
    with httpx.Client(transport=httpx.HTTPTransport(retries=0, trust_env=False),
            timeout=timeout, trust_env=False, follow_redirects=False) as client, \
            client.stream('POST', endpoint, headers=headers, content=payload) as response:
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            raise UnknownOutcome('response_encoding')
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body)+len(chunk) > maximum:
                raise UnknownOutcome('response_limit')
            body.extend(chunk)
        return HTTPResponse(response.status_code, bytes(body))


def _child(stdin: BinaryIO, stdout: BinaryIO) -> int:
    try:
        size = struct.unpack('!I', stdin.read(4))[0]
        if not 0 < size <= MAX_HEADER_BYTES:
            return 2
        header = _json(stdin.read(size))
        if (set(header) != {'endpoint', 'headers', 'timeout', 'maximum', 'size'}
                or type(header['size']) is not int or not 0 < header['size'] <= MAX_REQUEST_BYTES):
            return 2
        payload = stdin.read(header['size']+1)
        if len(payload) != header['size']:
            return 2
        try:
            result = _post(header['endpoint'], payload, header['headers'], header['timeout'], header['maximum'])
        except (UnknownOutcome, httpx.HTTPError) as exc:
            # Emit only a fixed classification, never the exception message,
            # request URL, provider content or credential-bearing stderr.
            reason = exc.reason if isinstance(exc, UnknownOutcome) else next(
                (code for cls, code in ((httpx.ConnectTimeout, 'connect_timeout'),
                    (httpx.ReadTimeout, 'read_timeout'), (httpx.WriteTimeout, 'write_timeout'),
                    (httpx.PoolTimeout, 'pool_timeout'), (httpx.ConnectError, 'connect_error'))
                    if isinstance(exc, cls)), 'http_transport')
            stdout.write(_frame({'error': reason}, b''))
            stdout.flush()
            return 0
        stdout.write(_frame({'status_code':result.status_code, 'size':len(result.body)}, result.body))
        stdout.flush()
        return 0
    except Exception:  # noqa: BLE001 -- a child error never exposes credential-bearing diagnostics.
        return 2


if __name__ == '__main__':
    sys.exit(_child(sys.stdin.buffer, sys.stdout.buffer) if sys.argv[1:] == ['--child'] else 2)
