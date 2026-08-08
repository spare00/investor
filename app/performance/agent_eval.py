"""Agent prediction evaluation and role-specific scoring."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Sequence

from app.performance.calibration import calibration_gap, expected_calibration_error
from app.performance.types import MetricResult, MetricStatus, metric_result


class Direction(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    ABSTAIN = "ABSTAIN"


@dataclass(slots=True)
class AgentPrediction:
    predicted_direction: Direction | str
    confidence: float | None  # 0-1
    actual_return: float | None
    abstained: bool = False
    universe_horizon: str | None = None
    agent_name: str | None = None


def _dir_correct(pred: Direction, actual_return: float) -> bool | None:
    if pred == Direction.ABSTAIN or pred == Direction.NEUTRAL:
        return None
    if pred == Direction.BULLISH:
        return actual_return > 0
    if pred == Direction.BEARISH:
        return actual_return < 0
    return None


def directional_accuracy(predictions: Sequence[AgentPrediction]) -> MetricResult:
    scored = [
        _dir_correct(Direction(p.predicted_direction), p.actual_return)
        for p in predictions
        if p.actual_return is not None and not p.abstained
        and Direction(p.predicted_direction) not in {Direction.ABSTAIN, Direction.NEUTRAL}
    ]
    scored = [s for s in scored if s is not None]
    if not scored:
        return metric_result(
            "directional_accuracy",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            method="agent_eval",
        )
    return metric_result(
        "directional_accuracy",
        sum(1 for s in scored if s) / len(scored),
        observation_count=len(scored),
        method="agent_eval",
    )


def brier_score(predictions: Sequence[AgentPrediction]) -> MetricResult:
    pairs = [
        (p.confidence, 1.0 if p.actual_return and p.actual_return > 0 else 0.0)
        for p in predictions
        if p.confidence is not None and p.actual_return is not None and not p.abstained
    ]
    if not pairs:
        return metric_result("brier_score", None, status=MetricStatus.INSUFFICIENT_DATA, method="agent_eval")
    score = sum((c - o) ** 2 for c, o in pairs) / len(pairs)
    return metric_result("brier_score", score, observation_count=len(pairs), method="agent_eval")


def calibration_buckets(
    predictions: Sequence[AgentPrediction],
    *,
    min_sample_size: int = 5,
) -> dict[str, Any]:
    conf_outcome = [
        (p.confidence, 1.0 if p.actual_return and p.actual_return > 0 else 0.0)
        for p in predictions
        if p.confidence is not None and p.actual_return is not None and not p.abstained
    ]
    edges = [(0, 20), (21, 40), (41, 60), (61, 80), (81, 100)]
    buckets: list[dict[str, Any]] = []
    for lo, hi in edges:
        lo_f, hi_f = lo / 100.0, hi / 100.0
        in_bucket = [(c, o) for c, o in conf_outcome if lo_f <= c <= hi_f]
        if len(in_bucket) < min_sample_size:
            buckets.append(
                {
                    "range": f"{lo}-{hi}",
                    "count": len(in_bucket),
                    "avg_confidence": None,
                    "accuracy": None,
                    "status": MetricStatus.INSUFFICIENT_DATA.value,
                }
            )
        else:
            buckets.append(
                {
                    "range": f"{lo}-{hi}",
                    "count": len(in_bucket),
                    "avg_confidence": statistics.mean(c for c, _ in in_bucket),
                    "accuracy": statistics.mean(o for _, o in in_bucket),
                    "status": MetricStatus.AVAILABLE.value,
                }
            )
    return {
        "buckets": buckets,
        "calibration_gap": calibration_gap(conf_outcome, min_sample_size=min_sample_size),
        "ece": expected_calibration_error(conf_outcome, min_sample_size=min_sample_size),
    }


def score_risk_manager(
    *,
    blocked_trade_pnl: float | None,
    portfolio_pnl_without_block: float | None,
    loss_avoided: bool,
) -> MetricResult:
    """Credit useful loss avoidance, not just raw PnL."""
    if blocked_trade_pnl is None:
        return metric_result(
            "risk_manager_score",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            method="loss_avoidance",
        )
    score = 0.0
    if loss_avoided and blocked_trade_pnl < 0:
        score = min(1.0, abs(blocked_trade_pnl) / max(abs(portfolio_pnl_without_block or 1.0), 1.0))
    elif not loss_avoided and blocked_trade_pnl > 0:
        score = -0.5
    return metric_result("risk_manager_score", score, method="loss_avoidance")


def score_devil_advocate(
    *,
    counterargument_valid: bool,
    thesis_failed: bool,
    dissent_recorded: bool,
) -> MetricResult:
    """Credit useful counterarguments independent of trade PnL."""
    if not dissent_recorded:
        return metric_result(
            "devil_advocate_score",
            None,
            status=MetricStatus.INSUFFICIENT_DATA,
            method="counterargument_value",
        )
    score = 0.0
    if counterargument_valid and thesis_failed:
        score = 1.0
    elif counterargument_valid:
        score = 0.6
    elif thesis_failed:
        score = 0.2
    return metric_result("devil_advocate_score", score, method="counterargument_value")


def evaluate_agents(predictions: Sequence[AgentPrediction]) -> dict[str, Any]:
    abstentions = sum(1 for p in predictions if p.abstained)
    return {
        "directional_accuracy": directional_accuracy(predictions),
        "brier_score": brier_score(predictions),
        "calibration": calibration_buckets(predictions),
        "abstention_count": abstentions,
        "abstention_rate": metric_result(
            "abstention_rate",
            abstentions / len(predictions) if predictions else None,
            observation_count=len(predictions),
            status=MetricStatus.AVAILABLE if predictions else MetricStatus.INSUFFICIENT_DATA,
            method="agent_eval",
        ),
        "prediction_count": len(predictions),
    }


def _bucket_key(value: str | None, *, allowed: tuple[str, ...], fallback: str = "unknown") -> str:
    key = (value or fallback).lower()
    return key if key in allowed else fallback


def group_predictions(
    predictions: Sequence[AgentPrediction],
    *,
    by: str,
) -> dict[str, list[AgentPrediction]]:
    """Group predictions by universe_horizon or agent_name."""
    if by == "universe_horizon":
        books = ("scalp", "day", "short", "medium", "unknown")
        buckets: dict[str, list[AgentPrediction]] = {b: [] for b in books}
        for p in predictions:
            buckets[_bucket_key(p.universe_horizon, allowed=books)].append(p)
        return buckets
    if by == "agent_name":
        buckets = {}
        for p in predictions:
            key = (p.agent_name or "unknown").lower()
            buckets.setdefault(key, []).append(p)
        return buckets
    raise ValueError(f"unsupported_group_by:{by}")


def evaluate_agents_grouped(
    predictions: Sequence[AgentPrediction],
    *,
    by: str,
) -> dict[str, dict[str, Any]]:
    return {key: evaluate_agents(items) for key, items in group_predictions(predictions, by=by).items()}
