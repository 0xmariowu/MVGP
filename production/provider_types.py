"""What every generation adapter hands the worker (fal, apilio): the resolved reference files and the receipt."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResolvedReference:
    object_ref: dict[str, Any]
    path: Path
    sha256: str
    media_type: str


@dataclass(frozen=True)
class ProviderReceipt:
    job_id: str
    job_type: str
    provider_status: str
    state: str
    raw_receipt: dict[str, Any]
    adjustments: dict[str, Any]
    critical_adjustments: dict[str, Any]
    result_url: str | None = field(default=None, repr=False)
    settled_cost: int | None = None  # None: the provider reported no reliable charge; the reservation holds.
    parameter_reports: dict[str, Any] = field(default_factory=dict)
