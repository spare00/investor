"""Maximum Adverse / Favorable Excursion for long positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.performance.types import MetricResult, MetricStatus, metric_result


@dataclass(slots=True)
class MaeMfeResult:
    mae: MetricResult
    mfe: MetricResult
    mae_pct: MetricResult
    mfe_pct: MetricResult
    mae_r: MetricResult
    mfe_r: MetricResult
    capture_ratio: MetricResult
    giveback_ratio: MetricResult


PricePoint = tuple[float, float]  # (timestamp_or_index, price)


def compute_mae_mfe(
    *,
    entry_price: float,
    exit_price: float,
    stop_distance: float | None,
    prices_during_hold: Sequence[PricePoint],
) -> MaeMfeResult:
    unavailable = lambda name: metric_result(name, None, status=MetricStatus.UNAVAILABLE, method="long_mae_mfe")
    insufficient = lambda name: metric_result(
        name, None, status=MetricStatus.INSUFFICIENT_DATA, method="long_mae_mfe"
    )

    if not prices_during_hold or entry_price <= 0:
        return MaeMfeResult(
            mae=insufficient("mae"),
            mfe=insufficient("mfe"),
            mae_pct=insufficient("mae_pct"),
            mfe_pct=insufficient("mfe_pct"),
            mae_r=unavailable("mae_r") if stop_distance else insufficient("mae_r"),
            mfe_r=unavailable("mfe_r") if stop_distance else insufficient("mfe_r"),
            capture_ratio=insufficient("capture_ratio"),
            giveback_ratio=insufficient("giveback_ratio"),
        )

    lows = [p for _, p in prices_during_hold]
    highs = lows
    min_price = min(lows)
    max_price = max(highs)

    mae_val = entry_price - min_price
    mfe_val = max_price - entry_price
    mae_pct = mae_val / entry_price
    mfe_pct = mfe_val / entry_price

    realized = exit_price - entry_price
    capture = realized / mfe_val if mfe_val > 0 else None
    giveback = (mfe_val - max(0.0, realized)) / mfe_val if mfe_val > 0 else None

    def ok(name: str, val: float | None, *, count: int) -> MetricResult:
        return metric_result(name, val, observation_count=count, method="long_mae_mfe")

    n = len(prices_during_hold)
    mae_r = mfe_r = unavailable("mae_r")
    if stop_distance and stop_distance > 0:
        mae_r = ok("mae_r", mae_val / stop_distance, count=n)
        mfe_r = ok("mfe_r", mfe_val / stop_distance, count=n)

    return MaeMfeResult(
        mae=ok("mae", mae_val, count=n),
        mfe=ok("mfe", mfe_val, count=n),
        mae_pct=ok("mae_pct", mae_pct, count=n),
        mfe_pct=ok("mfe_pct", mfe_pct, count=n),
        mae_r=mae_r,
        mfe_r=mfe_r,
        capture_ratio=ok("capture_ratio", capture, count=n),
        giveback_ratio=ok("giveback_ratio", giveback, count=n),
    )
