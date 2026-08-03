"""Drawdown analysis from equity curves."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Sequence

from app.performance.types import MetricResult, MetricStatus, metric_result


class DrawdownStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RECOVERED = "RECOVERED"


@dataclass(slots=True)
class DrawdownPeriod:
    peak_at: datetime
    peak_value: float
    trough_at: datetime
    trough_value: float
    recovered_at: datetime | None
    drawdown_pct: float
    duration_days: int
    recovery_days: int | None
    status: DrawdownStatus


EquityPoint = tuple[datetime, float]


def _days_between(a: datetime, b: datetime) -> int:
    return max(0, int((b - a).total_seconds() // 86400))


def compute_drawdowns(equity_curve: Sequence[EquityPoint]) -> list[DrawdownPeriod]:
    if len(equity_curve) < 2:
        return []
    periods: list[DrawdownPeriod] = []
    peak_val = equity_curve[0][1]
    peak_at = equity_curve[0][0]
    trough_val = peak_val
    trough_at = peak_at
    in_dd = False

    for dt, val in equity_curve[1:]:
        if val >= peak_val:
            if in_dd:
                periods.append(
                    DrawdownPeriod(
                        peak_at=peak_at,
                        peak_value=peak_val,
                        trough_at=trough_at,
                        trough_value=trough_val,
                        recovered_at=dt,
                        drawdown_pct=(trough_val - peak_val) / peak_val if peak_val else 0.0,
                        duration_days=_days_between(peak_at, trough_at),
                        recovery_days=_days_between(trough_at, dt),
                        status=DrawdownStatus.RECOVERED,
                    )
                )
                in_dd = False
            peak_val, peak_at = val, dt
            trough_val, trough_at = val, dt
        else:
            in_dd = True
            if val < trough_val:
                trough_val, trough_at = val, dt

    if in_dd and peak_val > 0:
        periods.append(
            DrawdownPeriod(
                peak_at=peak_at,
                peak_value=peak_val,
                trough_at=trough_at,
                trough_value=trough_val,
                recovered_at=None,
                drawdown_pct=(trough_val - peak_val) / peak_val,
                duration_days=_days_between(peak_at, trough_at),
                recovery_days=None,
                status=DrawdownStatus.ACTIVE,
            )
        )
    return periods


def max_drawdown(equity_curve: Sequence[EquityPoint]) -> MetricResult:
    dds = compute_drawdowns(equity_curve)
    if not dds:
        return metric_result(
            "max_drawdown",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(equity_curve),
            method="peak_to_trough",
        )
    worst = min(dds, key=lambda d: d.drawdown_pct)
    return metric_result(
        "max_drawdown",
        worst.drawdown_pct,
        period_start=worst.peak_at,
        period_end=worst.trough_at,
        observation_count=len(equity_curve),
        method="peak_to_trough",
    )


def current_drawdown(equity_curve: Sequence[EquityPoint]) -> MetricResult:
    if not equity_curve:
        return metric_result(
            "current_drawdown",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            method="peak_to_current",
        )
    peak_val = max(v for _, v in equity_curve)
    _, current = equity_curve[-1]
    if peak_val == 0:
        return metric_result(
            "current_drawdown",
            None,
            status=MetricStatus.UNRELIABLE,
            observation_count=len(equity_curve),
            method="peak_to_current",
        )
    return metric_result(
        "current_drawdown",
        (current - peak_val) / peak_val,
        period_start=equity_curve[0][0],
        period_end=equity_curve[-1][0],
        observation_count=len(equity_curve),
        method="peak_to_current",
    )
