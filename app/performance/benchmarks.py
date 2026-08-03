"""Benchmark series loading and aligned return computation.

Uses adjusted close when available in fixture data. When only unadjusted
close is supplied, corporate actions (splits, dividends) are NOT modeled —
aligned returns may diverge from index total-return benchmarks. Callers should
prefer total-return or adjusted series for relative performance metrics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from app.performance.returns import daily_returns, simple_return
from app.performance.types import MetricResult, MetricStatus, metric_result

EquityPoint = tuple[datetime, float]

# Built-in fixture benchmarks (date string -> close)
FIXTURE_BENCHMARKS: dict[str, dict[str, float]] = {
    "SPY": {
        "2024-01-02": 100.0,
        "2024-01-03": 100.5,
        "2024-01-04": 99.8,
        "2024-01-05": 101.2,
    },
    "QQQ": {
        "2024-01-02": 200.0,
        "2024-01-03": 201.0,
        "2024-01-04": 199.5,
        "2024-01-05": 202.0,
    },
}


def load_benchmark_series(
    name: str,
    *,
    source: dict[str, float] | None = None,
) -> list[EquityPoint]:
    """Load benchmark from explicit dict or built-in fixture."""
    raw = source if source is not None else FIXTURE_BENCHMARKS.get(name.upper(), {})
    points: list[EquityPoint] = []
    for date_str, close in sorted(raw.items()):
        dt = datetime.fromisoformat(date_str)
        points.append((dt, close))
    return points


def benchmark_period_return(series: Sequence[EquityPoint]) -> MetricResult:
    if len(series) < 2:
        return metric_result(
            "benchmark_return",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(series),
            method="simple",
        )
    start_dt, start = series[0]
    end_dt, end = series[-1]
    return metric_result(
        "benchmark_return",
        simple_return(start, end),
        period_start=start_dt,
        period_end=end_dt,
        observation_count=len(series),
        method="simple_unadjusted",
    )


def aligned_benchmark_returns(
    portfolio_dates: Sequence[datetime],
    benchmark_series: Sequence[EquityPoint],
) -> list[tuple[datetime, float]] | None:
    """Return daily benchmark returns aligned to portfolio dates; None if gaps."""
    bench_daily = daily_returns(benchmark_series)
    bench_map = {dt.date(): r for dt, r in bench_daily}
    aligned: list[tuple[datetime, float]] = []
    for dt in portfolio_dates:
        key = dt.date()
        if key not in bench_map:
            return None
        aligned.append((dt, bench_map[key]))
    return aligned


def load_and_align(
    benchmark_name: str,
    portfolio_equity: Sequence[EquityPoint],
    *,
    source: dict[str, float] | None = None,
) -> dict[str, Any]:
    series = load_benchmark_series(benchmark_name, source=source)
    port_daily = daily_returns(portfolio_equity)
    port_dates = [dt for dt, _ in port_daily]
    aligned = aligned_benchmark_returns(port_dates, series)
    return {
        "benchmark_name": benchmark_name,
        "series": series,
        "period_return": benchmark_period_return(series),
        "aligned_returns": aligned,
        "adjusted_close_limitation": (
            "Returns computed from supplied close prices; splits/dividends not "
            "applied unless caller provides adjusted series."
        ),
    }
