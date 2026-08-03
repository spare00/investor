"""Risk and reward metrics — all return MetricResult."""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Sequence

from app.performance.returns import cumulative_return
from app.performance.types import (
    ANNUALIZATION_FACTOR,
    DEFAULT_MIN_OBS,
    MetricResult,
    MetricStatus,
    metric_result,
)


def _period_bounds(returns: Sequence[float]) -> tuple[datetime | None, datetime | None]:
    return None, None


def cagr(
    start_value: float,
    end_value: float,
    *,
    years: float,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> MetricResult:
    if years <= 0 or start_value <= 0:
        return metric_result(
            "cagr",
            None,
            status=MetricStatus.UNRELIABLE if start_value <= 0 else MetricStatus.INSUFFICIENT_DATA,
            period_start=period_start,
            period_end=period_end,
            method="geometric",
        )
    value = (end_value / start_value) ** (1.0 / years) - 1.0
    return metric_result(
        "cagr",
        value,
        period_start=period_start,
        period_end=period_end,
        annualization_factor=1.0 / years,
        method="geometric",
    )


def annualized_volatility(
    returns: Sequence[float],
    *,
    min_obs: int = DEFAULT_MIN_OBS,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> MetricResult:
    if len(returns) < min_obs:
        return metric_result(
            "annualized_volatility",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            period_start=period_start,
            period_end=period_end,
            observation_count=len(returns),
            annualization_factor=ANNUALIZATION_FACTOR,
            method="sample_std",
        )
    vol = statistics.stdev(returns) * math.sqrt(ANNUALIZATION_FACTOR)
    return metric_result(
        "annualized_volatility",
        vol,
        period_start=period_start,
        period_end=period_end,
        observation_count=len(returns),
        annualization_factor=ANNUALIZATION_FACTOR,
        method="sample_std",
    )


def sharpe_ratio(
    returns: Sequence[float],
    *,
    risk_free_rate: float = 0.0,
    min_obs: int = DEFAULT_MIN_OBS,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> MetricResult:
    if len(returns) < min_obs:
        return metric_result(
            "sharpe_ratio",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            period_start=period_start,
            period_end=period_end,
            observation_count=len(returns),
            risk_free_rate=risk_free_rate,
            method="excess_return_over_vol",
        )
    if len(returns) == 1:
        vol = 0.0
    else:
        vol = statistics.stdev(returns)
    if vol == 0:
        return metric_result(
            "sharpe_ratio",
            None,
            status=MetricStatus.UNRELIABLE,
            period_start=period_start,
            period_end=period_end,
            observation_count=len(returns),
            risk_free_rate=risk_free_rate,
            method="excess_return_over_vol",
        )
    rf_daily = risk_free_rate / ANNUALIZATION_FACTOR
    excess = [r - rf_daily for r in returns]
    mean_excess = statistics.mean(excess)
    value = (mean_excess / vol) * math.sqrt(ANNUALIZATION_FACTOR)
    return metric_result(
        "sharpe_ratio",
        value,
        period_start=period_start,
        period_end=period_end,
        observation_count=len(returns),
        annualization_factor=ANNUALIZATION_FACTOR,
        risk_free_rate=risk_free_rate,
        method="excess_return_over_vol",
    )


def downside_deviation(
    returns: Sequence[float],
    *,
    mar: float = 0.0,
    min_obs: int = DEFAULT_MIN_OBS,
) -> MetricResult:
    if len(returns) < min_obs:
        return metric_result(
            "downside_deviation",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(returns),
            method="semi_deviation",
        )
    downside = [min(0.0, r - mar) ** 2 for r in returns]
    if not any(d > 0 for d in downside):
        return metric_result(
            "downside_deviation",
            0.0,
            observation_count=len(returns),
            annualization_factor=ANNUALIZATION_FACTOR,
            method="semi_deviation",
        )
    dd = math.sqrt(sum(downside) / len(returns)) * math.sqrt(ANNUALIZATION_FACTOR)
    return metric_result(
        "downside_deviation",
        dd,
        observation_count=len(returns),
        annualization_factor=ANNUALIZATION_FACTOR,
        method="semi_deviation",
    )


def sortino_ratio(
    returns: Sequence[float],
    *,
    risk_free_rate: float = 0.0,
    min_obs: int = DEFAULT_MIN_OBS,
) -> MetricResult:
    dd = downside_deviation(returns, mar=risk_free_rate / ANNUALIZATION_FACTOR, min_obs=min_obs)
    if dd.status != MetricStatus.AVAILABLE or dd.value is None or dd.value == 0:
        status = dd.status if dd.value != 0 else MetricStatus.UNRELIABLE
        return metric_result(
            "sortino_ratio",
            None,
            status=status,
            observation_count=len(returns),
            risk_free_rate=risk_free_rate,
            method="excess_over_downside",
        )
    rf_daily = risk_free_rate / ANNUALIZATION_FACTOR
    mean_excess = statistics.mean(returns) - rf_daily
    value = (mean_excess / (dd.value / math.sqrt(ANNUALIZATION_FACTOR))) * math.sqrt(ANNUALIZATION_FACTOR)
    return metric_result(
        "sortino_ratio",
        value,
        observation_count=len(returns),
        annualization_factor=ANNUALIZATION_FACTOR,
        risk_free_rate=risk_free_rate,
        method="excess_over_downside",
    )


def calmar_ratio(
    returns: Sequence[float],
    max_drawdown_pct: float,
    *,
    min_obs: int = DEFAULT_MIN_OBS,
) -> MetricResult:
    if len(returns) < min_obs:
        return metric_result(
            "calmar_ratio",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(returns),
            method="ann_return_over_max_dd",
        )
    if max_drawdown_pct == 0:
        return metric_result(
            "calmar_ratio",
            None,
            status=MetricStatus.UNRELIABLE,
            observation_count=len(returns),
            method="ann_return_over_max_dd",
        )
    ann = statistics.mean(returns) * ANNUALIZATION_FACTOR
    return metric_result(
        "calmar_ratio",
        ann / abs(max_drawdown_pct),
        observation_count=len(returns),
        annualization_factor=ANNUALIZATION_FACTOR,
        method="ann_return_over_max_dd",
    )


def historical_var(
    returns: Sequence[float],
    *,
    confidence: float = 0.95,
    min_obs: int = DEFAULT_MIN_OBS,
) -> MetricResult:
    if len(returns) < min_obs:
        return metric_result(
            "historical_var",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(returns),
            method=f"historical_{confidence:.0%}",
        )
    sorted_rets = sorted(returns)
    idx = max(0, int((1.0 - confidence) * len(sorted_rets)) - 1)
    return metric_result(
        "historical_var",
        -sorted_rets[idx],
        observation_count=len(returns),
        method=f"historical_{confidence:.0%}",
    )


def expected_shortfall(
    returns: Sequence[float],
    *,
    confidence: float = 0.95,
    min_obs: int = DEFAULT_MIN_OBS,
) -> MetricResult:
    if len(returns) < min_obs:
        return metric_result(
            "expected_shortfall",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(returns),
            method=f"es_{confidence:.0%}",
        )
    sorted_rets = sorted(returns)
    cutoff = max(1, int((1.0 - confidence) * len(sorted_rets)))
    tail = sorted_rets[:cutoff]
    value = -statistics.mean(tail) if tail else None
    return metric_result(
        "expected_shortfall",
        value,
        observation_count=len(returns),
        method=f"es_{confidence:.0%}",
    )


def beta(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    min_obs: int = DEFAULT_MIN_OBS,
    benchmark_name: str | None = None,
) -> MetricResult:
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < min_obs:
        return metric_result(
            "beta",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=n,
            benchmark=benchmark_name,
            method="ols_covariance",
        )
    p = portfolio_returns[:n]
    b = benchmark_returns[:n]
    mean_p = statistics.mean(p)
    mean_b = statistics.mean(b)
    cov = sum((pi - mean_p) * (bi - mean_b) for pi, bi in zip(p, b)) / (n - 1) if n > 1 else 0.0
    var_b = statistics.variance(b) if n > 1 else 0.0
    if var_b == 0:
        return metric_result(
            "beta",
            None,
            status=MetricStatus.UNRELIABLE,
            observation_count=n,
            benchmark=benchmark_name,
            method="ols_covariance",
        )
    return metric_result(
        "beta",
        cov / var_b,
        observation_count=n,
        benchmark=benchmark_name,
        method="ols_covariance",
    )


def alpha(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    risk_free_rate: float = 0.0,
    min_obs: int = DEFAULT_MIN_OBS,
    benchmark_name: str | None = None,
) -> MetricResult:
    b = beta(portfolio_returns, benchmark_returns, min_obs=min_obs, benchmark_name=benchmark_name)
    if b.status != MetricStatus.AVAILABLE or b.value is None:
        return metric_result(
            "alpha",
            None,
            status=b.status,
            observation_count=b.observation_count,
            risk_free_rate=risk_free_rate,
            benchmark=benchmark_name,
            method="capm_alpha",
        )
    n = b.observation_count
    rf_daily = risk_free_rate / ANNUALIZATION_FACTOR
    mean_p = statistics.mean(portfolio_returns[:n])
    mean_b = statistics.mean(benchmark_returns[:n])
    value = (mean_p - rf_daily) - b.value * (mean_b - rf_daily)
    return metric_result(
        "alpha",
        value * ANNUALIZATION_FACTOR,
        observation_count=n,
        annualization_factor=ANNUALIZATION_FACTOR,
        risk_free_rate=risk_free_rate,
        benchmark=benchmark_name,
        method="capm_alpha",
    )


def tracking_error(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    min_obs: int = DEFAULT_MIN_OBS,
    benchmark_name: str | None = None,
) -> MetricResult:
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < min_obs:
        return metric_result(
            "tracking_error",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=n,
            benchmark=benchmark_name,
            method="active_return_std",
        )
    active = [portfolio_returns[i] - benchmark_returns[i] for i in range(n)]
    if n == 1:
        return metric_result(
            "tracking_error",
            0.0,
            observation_count=n,
            benchmark=benchmark_name,
            method="active_return_std",
        )
    te = statistics.stdev(active) * math.sqrt(ANNUALIZATION_FACTOR)
    return metric_result(
        "tracking_error",
        te,
        observation_count=n,
        annualization_factor=ANNUALIZATION_FACTOR,
        benchmark=benchmark_name,
        method="active_return_std",
    )


def information_ratio(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    min_obs: int = DEFAULT_MIN_OBS,
    benchmark_name: str | None = None,
) -> MetricResult:
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < min_obs:
        return metric_result(
            "information_ratio",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=n,
            benchmark=benchmark_name,
            method="active_return_over_te",
        )
    active = [portfolio_returns[i] - benchmark_returns[i] for i in range(n)]
    mean_active = statistics.mean(active)
    te = tracking_error(portfolio_returns, benchmark_returns, min_obs=min_obs, benchmark_name=benchmark_name)
    if te.status != MetricStatus.AVAILABLE or te.value is None or te.value == 0:
        status = te.status if te.value != 0 else MetricStatus.UNRELIABLE
        return metric_result(
            "information_ratio",
            None,
            status=status,
            observation_count=n,
            benchmark=benchmark_name,
            method="active_return_over_te",
        )
    ir = (mean_active * ANNUALIZATION_FACTOR) / te.value
    return metric_result(
        "information_ratio",
        ir,
        observation_count=n,
        annualization_factor=ANNUALIZATION_FACTOR,
        benchmark=benchmark_name,
        method="active_return_over_te",
    )
