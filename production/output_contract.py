"""Pure LOCAL delivered-media checks; no provider, billing or approval authority.

Quality thresholds come from the pinned profile, never supplier parameter echoes.
Numeric/schema bounds below limit configuration and arithmetic, not film quality.
Video geometry/duration must be measured by MediaStore, not container fallbacks.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from fractions import Fraction
from typing import Annotated, Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from production.contracts import Contract, DomainError, content_hash

MEASUREMENT_LIMIT = 2**31 - 1
MODALITIES = {'image/png':'image', 'image/jpeg':'image', 'image/webp':'image',
              'video/mp4':'video', 'video/quicktime':'video'}


class OutputPolicy(Contract):
    schema_version: Literal[1]
    authority: Literal['LOCAL']
    modality: Literal['image', 'video']
    minimum_edge: Literal['long', 'short']
    minimum_pixels_by_resolution: Annotated[dict[str, Annotated[int, Field(ge=1, le=32768)]], Field(min_length=1, max_length=8)]
    aspect_ratio_relative_tolerance: Annotated[float, Field(ge=0, le=0.1)]
    duration_tolerance_seconds: Annotated[float, Field(ge=0, le=60)] | None
    require_requested_audio: bool

    @model_validator(mode='before')
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int:
            raise ValueError('Version must be an integer')
        return value

    @model_validator(mode='after')
    def coherent(self) -> Self:
        if self.modality == 'image':
            if (self.minimum_edge != 'long' or self.duration_tolerance_seconds is not None
                    or self.require_requested_audio or not set(self.minimum_pixels_by_resolution) <= {'1k','2k','4k'}):
                raise ValueError('Image policy has incompatible measurements or mappings')
        elif (self.minimum_edge != 'short' or self.duration_tolerance_seconds is None
                or not self.require_requested_audio or not set(self.minimum_pixels_by_resolution) <= {'480p','720p','1080p'}):
            # 480p is the fal Seedance 2.5 draft take; the pick is completed to 1080p.
            raise ValueError('Video policy has incompatible measurements or mappings')
        return self


def validate_policy(policy: Any, *, modality: str | None = None,
                    resolutions: Sequence[str] | None = None) -> dict[str, Any]:
    """Validate one released profile and optional route coverage; never enable a route.

    Invalid configuration raises unsupported_route. Extra documented mappings are
    allowed, so documenting 720p does not add 720p to a route's supported set.
    """
    try:
        value = OutputPolicy.model_validate(policy).model_dump()
        if modality is not None and modality != value['modality']:
            raise ValueError('Route modality differs')
        if resolutions is not None and (isinstance(resolutions, (str, bytes)) or not resolutions
                or len(resolutions) > 8 or any(type(item) is not str for item in resolutions)
                or not set(resolutions) <= set(value['minimum_pixels_by_resolution'])):
            raise ValueError('Route resolution mapping is incomplete')
        return value
    except (ValidationError, TypeError, ValueError):
        raise DomainError('unsupported_route', 'Released output contract is missing, invalid or incomplete', field='output_contract') from None


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if (type(value) not in ((int,) if integer else (int, float))
            or not 0 < value <= MEASUREMENT_LIMIT or not math.isfinite(value)):
        return None
    return value


def _fraction(value: float) -> Fraction:
    # Decimal round-trip avoids a binary-float epsilon changing an inclusive
    # published boundary. No hidden tolerance is added to the quality policy.
    return Fraction(str(value))


def validate_request(request_params: Any, policy: Any) -> dict[str, Any]:
    """Extract output expectations; video audio defaults must already be frozen.

    Creative/other provider parameters are not interpreted here. Authoritative
    compiled-request validation remains the compiler/adapter's responsibility.
    """
    value = validate_policy(policy)
    try:
        if not isinstance(request_params, dict):
            raise TypeError
        resolution, ratio = request_params['resolution'], request_params['aspect_ratio']
        if type(resolution) is not str or resolution not in value['minimum_pixels_by_resolution']:
            raise ValueError
        if not isinstance(ratio, str) or not re.fullmatch(r'[1-9][0-9]{0,5}:[1-9][0-9]{0,5}', ratio):
            raise ValueError
        width, height = map(int, ratio.split(':'))
        if not Fraction(1, 100) <= Fraction(width, height) <= 100:
            raise ValueError
        result = {'modality':value['modality'], 'resolution':resolution, 'aspect_ratio':ratio,
                  'minimum_edge':value['minimum_edge'], 'minimum_pixels':value['minimum_pixels_by_resolution'][resolution],
                  'aspect_ratio_relative_tolerance':value['aspect_ratio_relative_tolerance']}
        if value['modality'] == 'video':
            duration, audio = _number(request_params.get('duration')), request_params.get('generate_audio')
            if duration is None or type(audio) is not bool:
                raise ValueError
            result.update(duration=duration, generate_audio=audio, duration_tolerance_seconds=value['duration_tolerance_seconds'])
        return result
    except (KeyError, TypeError, ValueError):
        raise DomainError('invalid_input', 'Output request needs mapped resolution, explicit ratio and frozen video duration/audio', field='output_contract.requested') from None


def check_result(request_params: Any, probe: Any, media_type: str, policy: Any) -> dict[str, Any]:
    """Return conformance evidence, not approval. Malformed probes fail with reasons.

    Missing/invalid released policy or requested settings raise DomainError before
    evaluation. No fake mode, container-duration fallback or native-pixel claim.
    """
    value = validate_policy(policy)
    requested = validate_request(request_params, value)
    measured = probe if isinstance(probe, dict) else {}
    reasons: list[dict[str, Any]] = []
    observed: dict[str, Any] = {'media_type':media_type if isinstance(media_type,str) and len(media_type) <= 64 else None}
    def fail(code: str, field: str, expected: Any, actual: Any) -> None:
        reasons.append({'code':code, 'field':field, 'expected':expected, 'observed':actual})
    if not isinstance(media_type,str) or MODALITIES.get(media_type) != value['modality']:
        fail('modality_mismatch','media_type',value['modality'],observed['media_type'])
    if value['modality'] == 'image':
        width, height = _number(measured.get('width'),integer=True), _number(measured.get('height'),integer=True)
        observed.update(width=width,height=height)
    else:
        width, height = _number(measured.get('display_width')), _number(measured.get('display_height'))
        # Less than one displayed pixel is not usable geometry; this also bounds
        # ratio arithmetic for malformed near-zero floating-point measurements.
        width = width if width is not None and width >= 1 else None
        height = height if height is not None and height >= 1 else None
        verified = measured.get('display_geometry_status') == 'verified'
        observed.update(display_width=width,display_height=height,display_geometry_status='verified' if verified else 'unknown')
        if not verified:
            fail('unverified_display_geometry','display_geometry_status','verified','unknown')
            width = height = None
        if measured.get('has_video') is not True:
            fail('missing_video_stream','has_video',True,False if measured.get('has_video') is False else None)
        duration = _number(measured.get('video_duration'))
        source = measured.get('video_duration_source')
        if source not in ('stream-duration','stream-ticks'):
            duration, source = None, 'unknown'
        audio = measured.get('has_audio') if type(measured.get('has_audio')) is bool else None
        observed.update(video_duration=duration,video_duration_source=source,has_audio=audio)
        if duration is None:
            fail('missing_video_duration','video_duration','verified positive video-stream duration',None)
        elif abs(_fraction(duration)-_fraction(requested['duration'])) > _fraction(value['duration_tolerance_seconds']):
            fail('duration_mismatch','video_duration',{'seconds':requested['duration'],'tolerance':value['duration_tolerance_seconds']},duration)
        if audio is None:
            fail('missing_audio_measurement','has_audio','verified stream presence',None)
        elif requested['generate_audio'] and not audio:
            fail('missing_requested_audio','has_audio',True,False)
    if width is None or height is None:
        fail('missing_dimensions','dimensions','verified positive dimensions',None)
    else:
        edge = max(width,height) if value['minimum_edge'] == 'long' else min(width,height)
        observed['checked_edge_pixels'] = edge
        if edge < requested['minimum_pixels']:
            fail('minimum_edge_mismatch',value['minimum_edge']+'_edge',requested['minimum_pixels'],edge)
        numerator, denominator = map(int, requested['aspect_ratio'].split(':'))
        error = abs((_fraction(width)/_fraction(height))/Fraction(numerator,denominator)-1)
        observed['aspect_ratio_relative_error'] = float(error)
        if error > _fraction(value['aspect_ratio_relative_tolerance']):
            fail('aspect_ratio_mismatch','display_aspect_ratio',{'ratio':requested['aspect_ratio'],
                'relative_tolerance':value['aspect_ratio_relative_tolerance']},float(_fraction(width)/_fraction(height)))
    return {'passed':not reasons, 'requested':requested, 'observed':observed, 'reasons':reasons,
            'policy_hash':content_hash(value), 'native_resolution_verified':False}
