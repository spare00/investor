"""Operational KPI aggregates from counters."""

from __future__ import annotations

from typing import Any

from app.performance.types import MetricResult, MetricStatus, metric_result


def _safe_div(num: float, denom: float) -> float | None:
    return num / denom if denom else None


def aggregate_operational_kpis(counters: dict[str, Any]) -> dict[str, MetricResult | Any]:
    """counters: jobs_total, jobs_success, jobs_failed, workflows_total, workflows_completed,
    alerts_fired, manual_interventions, uptime_seconds, window_seconds."""
    jobs_total = int(counters.get("jobs_total", 0))
    jobs_success = int(counters.get("jobs_success", 0))
    jobs_failed = int(counters.get("jobs_failed", 0))
    wf_total = int(counters.get("workflows_total", 0))
    wf_done = int(counters.get("workflows_completed", 0))
    alerts = int(counters.get("alerts_fired", 0))
    manual = int(counters.get("manual_interventions", 0))
    uptime = float(counters.get("uptime_seconds", 0))
    window = float(counters.get("window_seconds", 0))

    def rate(name: str, num: int, denom: int) -> MetricResult:
        val = _safe_div(float(num), float(denom))
        return metric_result(
            name,
            val,
            status=MetricStatus.AVAILABLE if val is not None else MetricStatus.INSUFFICIENT_DATA,
            observation_count=denom,
            method="operational_counters",
        )

    return {
        "job_success_rate": rate("job_success_rate", jobs_success, jobs_total),
        "job_failure_rate": rate("job_failure_rate", jobs_failed, jobs_total),
        "workflow_completion_rate": rate("workflow_completion_rate", wf_done, wf_total),
        "alert_rate_per_hour": metric_result(
            "alert_rate_per_hour",
            alerts / (window / 3600.0) if window > 0 else None,
            status=MetricStatus.AVAILABLE if window > 0 else MetricStatus.INSUFFICIENT_DATA,
            method="operational_counters",
        ),
        "manual_intervention_rate": rate("manual_intervention_rate", manual, wf_total or jobs_total),
        "uptime_pct": metric_result(
            "uptime_pct",
            uptime / window if window > 0 else None,
            status=MetricStatus.AVAILABLE if window > 0 else MetricStatus.INSUFFICIENT_DATA,
            method="operational_counters",
        ),
    }
