"""Pure return calculations — no imputation of missing data."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Sequence

from app.performance.types import (
    ANNUALIZATION_FACTOR,
    CALCULATION_VERSION,
    MetricResult,
    MetricStatus,
    metric_result,
)

EquityPoint = tuple[datetime, float]
CashFlow = tuple[datetime, float]


def simple_return(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return (end - start) / start


def daily_returns(equity_series: Sequence[EquityPoint]) -> list[tuple[datetime, float]]:
    if len(equity_series) < 2:
        return []
    out: list[tuple[datetime, float]] = []
    for i in range(1, len(equity_series)):
        prev_dt, prev_val = equity_series[i - 1]
        dt, val = equity_series[i]
        if prev_val == 0:
            out.append((dt, 0.0))
        else:
            out.append((dt, (val - prev_val) / prev_val))
    return out


def time_weighted_return(period_returns: Sequence[float]) -> MetricResult:
    if not period_returns:
        return metric_result(
            "time_weighted_return",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=0,
            method="chain_link",
        )
    compound = 1.0
    for r in period_returns:
        compound *= 1.0 + r
    return metric_result(
        "time_weighted_return",
        compound - 1.0,
        observation_count=len(period_returns),
        method="chain_link",
    )


def _irr_bisection(cashflows: Sequence[tuple[float, float]], *, tol: float = 1e-8) -> float | None:
    """Solve NPV(rate)=0; cashflows as (time_years, amount)."""
    if len(cashflows) < 2:
        return None

    def npv(rate: float) -> float:
        return sum(amt / (1.0 + rate) ** t for t, amt in cashflows)

    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def money_weighted_return(
    *,
    start_value: float,
    end_value: float,
    period_start: datetime,
    period_end: datetime,
    cashflows: Sequence[CashFlow] | None = None,
    min_obs: int = 2,
) -> MetricResult:
    if cashflows is None or len(cashflows) < min_obs - 1:
        return metric_result(
            "money_weighted_return",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            period_start=period_start,
            period_end=period_end,
            observation_count=len(cashflows or []),
            method="irr",
        )
    span = max((period_end - period_start).total_seconds(), 1.0)
    flows: list[tuple[float, float]] = [(0.0, -start_value)]
    for dt, amt in cashflows:
        t = (dt - period_start).total_seconds() / span
        flows.append((t, amt))
    flows.append((1.0, end_value))
    rate = _irr_bisection(flows)
    if rate is None:
        return metric_result(
            "money_weighted_return",
            None,
            status=MetricStatus.UNRELIABLE,
            period_start=period_start,
            period_end=period_end,
            observation_count=len(cashflows) + 2,
            method="irr",
        )
    return metric_result(
        "money_weighted_return",
        rate,
        period_start=period_start,
        period_end=period_end,
        observation_count=len(cashflows) + 2,
        method="irr",
    )


def cumulative_return(period_returns: Sequence[float]) -> float:
    compound = 1.0
    for r in period_returns:
        compound *= 1.0 + r
    return compound - 1.0


def annualized_return(
    total_return: float,
    *,
    periods: int,
    periods_per_year: float = ANNUALIZATION_FACTOR,
) -> MetricResult:
    if periods < 1:
        return metric_result(
            "annualized_return",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=periods,
            annualization_factor=periods_per_year,
            method="geometric",
        )
    years = periods / periods_per_year
    if years <= 0:
        return metric_result(
            "annualized_return",
            None,
            status=MetricStatus.UNRELIABLE,
            observation_count=periods,
            annualization_factor=periods_per_year,
            method="geometric",
        )
    value = (1.0 + total_return) ** (1.0 / years) - 1.0
    return metric_result(
        "annualized_return",
        value,
        observation_count=periods,
        annualization_factor=periods_per_year,
        method="geometric",
    )


def _align_returns(
    portfolio: Sequence[tuple[datetime, float]],
    benchmark: Sequence[tuple[datetime, float]],
) -> tuple[list[float], list[float]] | None:
    bench_map = {dt.date(): r for dt, r in benchmark}
    p_rets: list[float] = []
    b_rets: list[float] = []
    for dt, r in portfolio:
        key = dt.date()
        if key not in bench_map:
            return None
        p_rets.append(r)
        b_rets.append(bench_map[key])
    return p_rets, b_rets


def excess_return(
    portfolio_returns: Sequence[tuple[datetime, float]],
    benchmark_returns: Sequence[tuple[datetime, float]],
    *,
    risk_free_rate: float = 0.0,
    benchmark_name: str | None = None,
) -> MetricResult:
    aligned = _align_returns(portfolio_returns, benchmark_returns)
    if aligned is None:
        return metric_result(
            "excess_return",
            None,
            status=MetricStatus.UNAVAILABLE,
            benchmark=benchmark_name,
            method="aligned_daily",
        )
    p_rets, b_rets = aligned
    if not p_rets:
        return metric_result(
            "excess_return",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            benchmark=benchmark_name,
            method="aligned_daily",
        )
    avg_excess = sum(p - b - risk_free_rate / ANNUALIZATION_FACTOR for p, b in zip(p_rets, b_rets)) / len(p_rets)
    return metric_result(
        "excess_return",
        avg_excess * ANNUALIZATION_FACTOR,
        observation_count=len(p_rets),
        annualization_factor=ANNUALIZATION_FACTOR,
        risk_free_rate=risk_free_rate,
        benchmark=benchmark_name,
        method="aligned_daily",
    )


def active_return(
    portfolio_returns: Sequence[tuple[datetime, float]],
    benchmark_returns: Sequence[tuple[datetime, float]],
    *,
    benchmark_name: str | None = None,
) -> MetricResult:
    aligned = _align_returns(portfolio_returns, benchmark_returns)
    if aligned is None:
        return metric_result(
            "active_return",
            None,
            status=MetricStatus.UNAVAILABLE,
            benchmark=benchmark_name,
            method="aligned_daily",
        )
    p_rets, b_rets = aligned
    if not p_rets:
        return metric_result(
            "active_return",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            benchmark=benchmark_name,
            method="aligned_daily",
        )
    p_cum = cumulative_return(p_rets)
    b_cum = cumulative_return(b_rets)
    return metric_result(
        "active_return",
        p_cum - b_cum,
        observation_count=len(p_rets),
        benchmark=benchmark_name,
        method="aligned_daily",
    )


def portfolio_absolute_return(
    equity_series: Sequence[EquityPoint],
) -> MetricResult:
    if len(equity_series) < 2:
        return metric_result(
            "portfolio_return",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            method="simple",
        )
    start_dt, start_val = equity_series[0]
    end_dt, end_val = equity_series[-1]
    return metric_result(
        "portfolio_return",
        simple_return(start_val, end_val),
        period_start=start_dt,
        period_end=end_dt,
        observation_count=len(equity_series),
        method="simple",
    )
