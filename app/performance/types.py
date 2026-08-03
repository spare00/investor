"""Shared types for deterministic performance calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

CALCULATION_VERSION = "1.0.0"
DEFAULT_MIN_OBS = 2
ANNUALIZATION_FACTOR = 252.0


class MetricStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNRELIABLE = "UNRELIABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(slots=True)
class MetricResult:
    metric_name: str
    value: float | None
    period_start: datetime | None
    period_end: datetime | None
    observation_count: int
    annualization_factor: float | None
    risk_free_rate: float | None
    benchmark: str | None
    method: str
    calculation_version: str
    data_quality: str | None
    status: MetricStatus


def metric_result(
    metric_name: str,
    value: float | None,
    *,
    status: MetricStatus = MetricStatus.AVAILABLE,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    observation_count: int = 0,
    annualization_factor: float | None = None,
    risk_free_rate: float | None = None,
    benchmark: str | None = None,
    method: str = "",
    data_quality: str | None = None,
) -> MetricResult:
    return MetricResult(
        metric_name=metric_name,
        value=value,
        period_start=period_start,
        period_end=period_end,
        observation_count=observation_count,
        annualization_factor=annualization_factor,
        risk_free_rate=risk_free_rate,
        benchmark=benchmark,
        method=method,
        calculation_version=CALCULATION_VERSION,
        data_quality=data_quality,
        status=status,
    )
