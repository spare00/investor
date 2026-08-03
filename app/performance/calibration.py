"""Probability calibration metrics."""

from __future__ import annotations

import statistics
from typing import Sequence

from app.performance.types import MetricResult, MetricStatus, metric_result

ConfidenceOutcome = tuple[float, float]  # (confidence 0-1, outcome 0|1)


def bucket_accuracy(
    samples: Sequence[ConfidenceOutcome],
    *,
    bucket_lo: float,
    bucket_hi: float,
    min_sample_size: int = 5,
) -> MetricResult:
    in_bucket = [(c, o) for c, o in samples if bucket_lo <= c <= bucket_hi]
    if len(in_bucket) < min_sample_size:
        return metric_result(
            "bucket_accuracy",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(in_bucket),
            method="calibration_bucket",
        )
    return metric_result(
        "bucket_accuracy",
        statistics.mean(o for _, o in in_bucket),
        observation_count=len(in_bucket),
        method="calibration_bucket",
    )


def calibration_gap(
    samples: Sequence[ConfidenceOutcome],
    *,
    min_sample_size: int = 5,
) -> MetricResult:
    if len(samples) < min_sample_size:
        return metric_result(
            "calibration_gap",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(samples),
            method="mean_conf_minus_accuracy",
        )
    avg_conf = statistics.mean(c for c, _ in samples)
    avg_acc = statistics.mean(o for _, o in samples)
    return metric_result(
        "calibration_gap",
        avg_conf - avg_acc,
        observation_count=len(samples),
        method="mean_conf_minus_accuracy",
    )


def expected_calibration_error(
    samples: Sequence[ConfidenceOutcome],
    *,
    n_bins: int = 5,
    min_sample_size: int = 5,
) -> MetricResult:
    if len(samples) < min_sample_size:
        return metric_result(
            "expected_calibration_error",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=len(samples),
            method="ece",
        )
    bins: list[list[ConfidenceOutcome]] = [[] for _ in range(n_bins)]
    for c, o in samples:
        idx = min(n_bins - 1, int(c * n_bins))
        bins[idx].append((c, o))
    total = len(samples)
    ece = 0.0
    used = 0
    for b in bins:
        if not b:
            continue
        avg_conf = statistics.mean(c for c, _ in b)
        avg_acc = statistics.mean(o for _, o in b)
        ece += (len(b) / total) * abs(avg_conf - avg_acc)
        used += len(b)
    if used < min_sample_size:
        return metric_result(
            "expected_calibration_error",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            observation_count=used,
            method="ece",
        )
    return metric_result(
        "expected_calibration_error",
        ece,
        observation_count=used,
        method="ece",
    )
