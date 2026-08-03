"""Execution quality metrics from order statistics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.performance.types import MetricResult, MetricStatus, metric_result


def _rate(name: str, num: int, denom: int) -> MetricResult:
    if denom <= 0:
        return metric_result(name, None, status=MetricStatus.INSUFFICIENT_DATA, method="order_stats")
    return metric_result(name, num / denom, observation_count=denom, method="order_stats")


def _latency_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds() * 1000.0


def compute_execution_quality(order_stats: dict[str, Any]) -> dict[str, MetricResult | float | None]:
    """order_stats keys: decision_at, submission_at, arrival_price, avg_fill_price,
    decision_price, total_orders, filled, partial, cancelled, rejected, latencies list."""
    decision_at = order_stats.get("decision_at")
    submission_at = order_stats.get("submission_at")
    first_fill_at = order_stats.get("first_fill_at")
    last_fill_at = order_stats.get("last_fill_at")

    arrival = order_stats.get("arrival_price")
    decision_price = order_stats.get("decision_price")
    avg_fill = order_stats.get("avg_fill_price")
    side = str(order_stats.get("side", "buy")).lower()

    total = int(order_stats.get("total_orders", 0))
    filled = int(order_stats.get("filled", 0))
    partial = int(order_stats.get("partial", 0))
    cancelled = int(order_stats.get("cancelled", 0))
    rejected = int(order_stats.get("rejected", 0))

    def unavailable(name: str) -> MetricResult:
        return metric_result(name, None, status=MetricStatus.UNAVAILABLE, method="order_stats")

    decision_to_submission = _latency_ms(decision_at, submission_at)
    submission_to_fill = _latency_ms(submission_at, first_fill_at)

    impl_shortfall: MetricResult
    if decision_price is not None and avg_fill is not None and decision_price != 0:
        sign = 1.0 if side == "buy" else -1.0
        impl_shortfall = metric_result(
            "implementation_shortfall",
            sign * (avg_fill - decision_price) / decision_price,
            method="decision_vs_fill",
        )
    else:
        impl_shortfall = unavailable("implementation_shortfall")

    slippage_bps: MetricResult
    if arrival is not None and avg_fill is not None and arrival != 0:
        sign = 1.0 if side == "buy" else -1.0
        slippage_bps = metric_result(
            "slippage_bps",
            sign * (avg_fill - arrival) / arrival * 10_000.0,
            method="arrival_vs_fill",
        )
    else:
        slippage_bps = unavailable("slippage_bps")

    latencies = order_stats.get("latencies_ms") or []
    avg_lat = sum(latencies) / len(latencies) if latencies else None
    p95_lat = sorted(latencies)[int(0.95 * (len(latencies) - 1))] if len(latencies) >= 2 else avg_lat

    return {
        "decision_to_submission_ms": decision_to_submission,
        "submission_to_fill_ms": submission_to_fill,
        "avg_fill_price": avg_fill,
        "arrival_price": arrival,
        "decision_price": decision_price,
        "implementation_shortfall": impl_shortfall,
        "slippage_bps": slippage_bps,
        "fill_rate": _rate("fill_rate", filled, total),
        "partial_rate": _rate("partial_rate", partial, total),
        "cancel_rate": _rate("cancel_rate", cancelled, total),
        "reject_rate": _rate("reject_rate", rejected, total),
        "avg_latency_ms": metric_result(
            "avg_latency_ms",
            avg_lat,
            status=MetricStatus.AVAILABLE if avg_lat is not None else MetricStatus.UNAVAILABLE,
            observation_count=len(latencies),
            method="order_stats",
        ),
        "p95_latency_ms": metric_result(
            "p95_latency_ms",
            p95_lat,
            status=MetricStatus.AVAILABLE if p95_lat is not None else MetricStatus.UNAVAILABLE,
            observation_count=len(latencies),
            method="order_stats",
        ),
    }
