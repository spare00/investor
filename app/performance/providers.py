"""Data provider reliability metrics from request statistics."""

from __future__ import annotations

import statistics
from typing import Any

from app.performance.types import MetricResult, MetricStatus, metric_result


def _rate(name: str, num: int, total: int) -> MetricResult:
    if total <= 0:
        return metric_result(name, None, status=MetricStatus.INSUFFICIENT_DATA, method="provider_stats")
    return metric_result(name, num / total, observation_count=total, method="provider_stats")


def compute_provider_reliability(stats: dict[str, Any]) -> dict[str, MetricResult | Any]:
    """stats: total_requests, successes, timeouts, errors, latencies_ms, freshness_seconds, last_success_at."""
    total = int(stats.get("total_requests", 0))
    successes = int(stats.get("successes", 0))
    timeouts = int(stats.get("timeouts", 0))
    errors = int(stats.get("errors", 0))
    latencies = stats.get("latencies_ms") or []
    freshness = stats.get("freshness_seconds")

    avg_lat = statistics.mean(latencies) if latencies else None
    p95_lat = sorted(latencies)[int(0.95 * (len(latencies) - 1))] if len(latencies) >= 2 else avg_lat

    availability = successes / total if total else None
    return {
        "availability": metric_result(
            "availability",
            availability,
            status=MetricStatus.AVAILABLE if availability is not None else MetricStatus.INSUFFICIENT_DATA,
            observation_count=total,
            method="provider_stats",
        ),
        "success_rate": _rate("success_rate", successes, total),
        "timeout_rate": _rate("timeout_rate", timeouts, total),
        "error_rate": _rate("error_rate", errors, total),
        "avg_latency_ms": metric_result(
            "avg_latency_ms",
            avg_lat,
            status=MetricStatus.AVAILABLE if avg_lat is not None else MetricStatus.UNAVAILABLE,
            observation_count=len(latencies),
            method="provider_stats",
        ),
        "p95_latency_ms": metric_result(
            "p95_latency_ms",
            p95_lat,
            status=MetricStatus.AVAILABLE if p95_lat is not None else MetricStatus.UNAVAILABLE,
            observation_count=len(latencies),
            method="provider_stats",
        ),
        "freshness_seconds": metric_result(
            "freshness_seconds",
            float(freshness) if freshness is not None else None,
            status=MetricStatus.AVAILABLE if freshness is not None else MetricStatus.UNAVAILABLE,
            method="provider_stats",
        ),
        "last_success_at": stats.get("last_success_at"),
    }
