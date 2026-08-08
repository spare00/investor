"""Trade-level performance metrics.

Default unit is Position Lifecycle — one closed lifecycle equals one trade
for win rate, expectancy, and R-multiple statistics unless callers aggregate
fills differently.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from app.performance.types import MetricResult, MetricStatus, metric_result


@dataclass(slots=True)
class ClosedTrade:
    pnl: float
    holding_minutes: float
    risk_amount: float | None = None
    fees: float = 0.0
    symbol: str | None = None
    horizon: str | None = None


def _status_for_empty(name: str) -> MetricResult:
    return metric_result(name, None, status=MetricStatus.INSUFFICIENT_DATA, method="position_lifecycle")


def _float_metric(name: str, value: float | None, *, count: int, method: str) -> MetricResult:
    return metric_result(name, value, observation_count=count, method=method)


def compute_trade_metrics(trades: list[ClosedTrade]) -> dict[str, MetricResult | Any]:
    if not trades:
        empty = _status_for_empty
        return {
            "win_rate": empty("win_rate"),
            "loss_rate": empty("loss_rate"),
            "avg_win": empty("avg_win"),
            "avg_loss": empty("avg_loss"),
            "largest_win": empty("largest_win"),
            "largest_loss": empty("largest_loss"),
            "profit_factor": empty("profit_factor"),
            "expectancy": empty("expectancy"),
            "payoff_ratio": empty("payoff_ratio"),
            "avg_holding_minutes": empty("avg_holding_minutes"),
            "median_holding_minutes": empty("median_holding_minutes"),
            "max_consecutive_wins": empty("max_consecutive_wins"),
            "max_consecutive_losses": empty("max_consecutive_losses"),
            "avg_risk_amount": empty("avg_risk_amount"),
            "avg_r_multiple": empty("avg_r_multiple"),
            "median_r_multiple": empty("median_r_multiple"),
            "trade_count": 0,
        }

    net_pnls = [t.pnl - t.fees for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    n = len(trades)

    win_rate = len(wins) / n
    loss_rate = len(losses) / n
    avg_win = statistics.mean(wins) if wins else None
    avg_loss = statistics.mean(losses) if losses else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    expectancy = statistics.mean(net_pnls)
    payoff = (avg_win / abs(avg_loss)) if avg_win and avg_loss else None

    holdings = [t.holding_minutes for t in trades]
    risks = [t.risk_amount for t in trades if t.risk_amount and t.risk_amount > 0]
    r_multiples = [
        (t.pnl - t.fees) / t.risk_amount
        for t in trades
        if t.risk_amount and t.risk_amount > 0
    ]

    max_w = max_cw = max_cl = cw = cl = 0
    for p in net_pnls:
        if p > 0:
            cw += 1
            cl = 0
            max_cw = max(max_cw, cw)
        elif p < 0:
            cl += 1
            cw = 0
            max_cl = max(max_cl, cl)
        else:
            cw = cl = 0

    method = "position_lifecycle"
    return {
        "win_rate": _float_metric("win_rate", win_rate, count=n, method=method),
        "loss_rate": _float_metric("loss_rate", loss_rate, count=n, method=method),
        "avg_win": _float_metric("avg_win", avg_win, count=len(wins), method=method),
        "avg_loss": _float_metric("avg_loss", avg_loss, count=len(losses), method=method),
        "largest_win": _float_metric("largest_win", max(wins) if wins else None, count=len(wins), method=method),
        "largest_loss": _float_metric("largest_loss", min(losses) if losses else None, count=len(losses), method=method),
        "profit_factor": _float_metric("profit_factor", profit_factor, count=n, method=method),
        "expectancy": _float_metric("expectancy", expectancy, count=n, method=method),
        "payoff_ratio": _float_metric("payoff_ratio", payoff, count=n, method=method),
        "avg_holding_minutes": _float_metric("avg_holding_minutes", statistics.mean(holdings), count=n, method=method),
        "median_holding_minutes": _float_metric(
            "median_holding_minutes", statistics.median(holdings), count=n, method=method
        ),
        "max_consecutive_wins": _float_metric("max_consecutive_wins", float(max_cw), count=n, method=method),
        "max_consecutive_losses": _float_metric("max_consecutive_losses", float(max_cl), count=n, method=method),
        "avg_risk_amount": _float_metric(
            "avg_risk_amount",
            statistics.mean(risks) if risks else None,
            count=len(risks),
            method=method,
        ),
        "avg_r_multiple": _float_metric(
            "avg_r_multiple",
            statistics.mean(r_multiples) if r_multiples else None,
            count=len(r_multiples),
            method=method,
        ),
        "median_r_multiple": _float_metric(
            "median_r_multiple",
            statistics.median(r_multiples) if r_multiples else None,
            count=len(r_multiples),
            method=method,
        ),
        "trade_count": n,
    }


def group_trade_metrics_by_horizon(
    trades: list[ClosedTrade],
    *,
    books: tuple[str, ...] = ("scalp", "day", "short", "medium", "unknown"),
) -> dict[str, dict[str, MetricResult | Any]]:
    """Firm-compatible per-book slices; empty books still return INSUFFICIENT_DATA metrics."""
    buckets: dict[str, list[ClosedTrade]] = {b: [] for b in books}
    for t in trades:
        key = (t.horizon or "unknown").lower()
        if key not in buckets:
            key = "unknown"
        buckets[key].append(t)
    return {book: compute_trade_metrics(items) for book, items in buckets.items()}
