"""Decision quality evaluation.

Look-ahead note: callers must supply only post-decision prices (horizon_price).
Do not pass future information beyond the evaluation horizon.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.performance.types import MetricResult, MetricStatus, metric_result


class DecisionAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


# Map CIO PortfolioAction / SymbolAction strings onto evaluation buckets.
_ACTION_ALIASES: dict[str, DecisionAction] = {
    "BUY": DecisionAction.BUY,
    "STRONG_BUY": DecisionAction.BUY,
    "ADD": DecisionAction.BUY,
    "SCALE_IN": DecisionAction.BUY,
    "SELL": DecisionAction.SELL,
    "STRONG_SELL": DecisionAction.SELL,
    "REDUCE": DecisionAction.SELL,
    "PARTIAL_SELL": DecisionAction.SELL,
    "CLOSE": DecisionAction.SELL,
    "CLOSE_LONG": DecisionAction.SELL,
    "HOLD": DecisionAction.HOLD,
    "NO_TRADE": DecisionAction.NO_TRADE,
    "STAY_CASH": DecisionAction.NO_TRADE,
    "WAIT": DecisionAction.NO_TRADE,
    "NO_NEW_RISK": DecisionAction.NO_TRADE,
    "NO_ACTION": DecisionAction.NO_TRADE,
}

# Coarse CIO time_horizon → universe book when watchlist stamp missing.
_CIO_TIME_TO_BOOK: dict[str, str] = {
    "intraday": "day",
    "swing": "short",
    "position": "medium",
}

_BOOKS = ("scalp", "day", "short", "medium", "unknown")


def normalize_decision_action(action: DecisionAction | str | None) -> DecisionAction:
    if isinstance(action, DecisionAction):
        return action
    if action is None:
        return DecisionAction.NO_TRADE
    key = str(action).strip().upper()
    if key in _ACTION_ALIASES:
        return _ACTION_ALIASES[key]
    try:
        return DecisionAction(key)
    except ValueError:
        # Unknown CIO action → treat as abstention rather than 500
        return DecisionAction.NO_TRADE


def universe_horizon_for_plan(
    plan: dict[str, Any] | None,
    *,
    watchlist_horizon: dict[str, str] | None = None,
) -> str:
    """Resolve scalp/day/short/medium/unknown for a symbol plan."""
    plan = plan or {}
    raw = plan.get("universe_horizon") or plan.get("horizon")
    if raw:
        key = str(raw).strip().lower()
        if key in _BOOKS:
            return key
    sym = str(plan.get("symbol") or "").upper()
    if sym and watchlist_horizon and sym in watchlist_horizon:
        key = str(watchlist_horizon[sym]).strip().lower()
        if key in _BOOKS:
            return key
    th = str(plan.get("time_horizon") or "").strip().lower()
    return _CIO_TIME_TO_BOOK.get(th, "unknown")


def evaluate_decision(
    *,
    decision_price: float,
    action: DecisionAction | str | None,
    horizon_price: float | None,
    benchmark_return: float | None = None,
) -> dict[str, MetricResult | Any]:
    raw_action = str(action) if action is not None else None
    action = normalize_decision_action(action)
    if horizon_price is None or decision_price <= 0:
        return {
            "action": action.value,
            "raw_action": raw_action,
            "realized_return": metric_result(
                "realized_return", None, status=MetricStatus.UNAVAILABLE, method="decision_eval"
            ),
            "vs_benchmark": metric_result(
                "vs_benchmark", None, status=MetricStatus.UNAVAILABLE, method="decision_eval"
            ),
            "directional_correct": metric_result(
                "directional_correct", None, status=MetricStatus.UNAVAILABLE, method="decision_eval"
            ),
            "abstention_quality": metric_result(
                "abstention_quality", None, status=MetricStatus.UNAVAILABLE, method="decision_eval"
            ),
            "quality_score": metric_result(
                "quality_score", None, status=MetricStatus.UNAVAILABLE, method="decision_eval"
            ),
        }

    realized = (horizon_price - decision_price) / decision_price
    vs_bench = (
        metric_result("vs_benchmark", realized - benchmark_return, method="decision_eval")
        if benchmark_return is not None
        else metric_result("vs_benchmark", None, status=MetricStatus.UNAVAILABLE, method="decision_eval")
    )

    directional: MetricResult
    if action == DecisionAction.BUY:
        directional = metric_result(
            "directional_correct", 1.0 if realized > 0 else 0.0, method="decision_eval"
        )
    elif action == DecisionAction.SELL:
        directional = metric_result(
            "directional_correct", 1.0 if realized < 0 else 0.0, method="decision_eval"
        )
    elif action == DecisionAction.HOLD:
        directional = metric_result(
            "directional_correct", 1.0 if abs(realized) < 0.005 else 0.0, method="decision_eval"
        )
    else:
        directional = metric_result(
            "directional_correct", None, status=MetricStatus.UNAVAILABLE, method="decision_eval"
        )

    abstention: MetricResult
    if action == DecisionAction.NO_TRADE:
        # Good abstention when move was small or adverse for a hypothetical long
        quality = 1.0 - min(1.0, abs(realized) / 0.02)
        abstention = metric_result("abstention_quality", quality, method="decision_eval")
    else:
        abstention = metric_result(
            "abstention_quality", None, status=MetricStatus.UNAVAILABLE, method="decision_eval"
        )

    if action == DecisionAction.BUY:
        score = realized
    elif action == DecisionAction.SELL:
        score = -realized
    elif action == DecisionAction.HOLD:
        score = -abs(realized)
    elif action == DecisionAction.NO_TRADE:
        score = abstention.value if abstention.value is not None else 0.0
    else:
        score = 0.0

    return {
        "action": action.value,
        "raw_action": raw_action,
        "realized_return": metric_result("realized_return", realized, method="decision_eval"),
        "vs_benchmark": vs_bench,
        "directional_correct": directional,
        "abstention_quality": abstention,
        "quality_score": metric_result("quality_score", score, method="decision_eval"),
    }


def _metric_value(metrics: dict[str, Any], key: str) -> float | None:
    m = metrics.get(key)
    if m is None:
        return None
    if isinstance(m, MetricResult):
        if m.status != MetricStatus.AVAILABLE or m.value is None:
            return None
        return float(m.value)
    if isinstance(m, dict):
        if m.get("status") not in (None, "AVAILABLE", MetricStatus.AVAILABLE.value):
            return None
        v = m.get("value")
        return float(v) if v is not None else None
    try:
        return float(m)
    except (TypeError, ValueError):
        return None


def summarize_decision_evaluations(
    evaluations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate directional hit-rate / quality by universe horizon book."""
    books = {
        b: {"count": 0, "scored": 0, "directional_hits": 0, "quality_sum": 0.0, "quality_n": 0}
        for b in _BOOKS
    }
    firm = {"count": 0, "scored": 0, "directional_hits": 0, "quality_sum": 0.0, "quality_n": 0}

    def _bump(bucket: dict[str, Any], metrics: dict[str, Any]) -> None:
        bucket["count"] += 1
        firm["count"] += 1
        dir_v = _metric_value(metrics, "directional_correct")
        if dir_v is not None:
            bucket["scored"] += 1
            firm["scored"] += 1
            if dir_v >= 0.5:
                bucket["directional_hits"] += 1
                firm["directional_hits"] += 1
        q = _metric_value(metrics, "quality_score")
        if q is not None:
            bucket["quality_sum"] += q
            bucket["quality_n"] += 1
            firm["quality_sum"] += q
            firm["quality_n"] += 1

    for item in evaluations:
        hz = str(item.get("universe_horizon") or "unknown").lower()
        if hz not in books:
            hz = "unknown"
        metrics = item.get("metrics") or {}
        _bump(books[hz], metrics)

    def _pack(bucket: dict[str, Any]) -> dict[str, Any]:
        scored = int(bucket["scored"])
        qn = int(bucket["quality_n"])
        return {
            "count": int(bucket["count"]),
            "scored": scored,
            "directional_hit_rate": (bucket["directional_hits"] / scored) if scored else None,
            "avg_quality_score": (bucket["quality_sum"] / qn) if qn else None,
        }

    return {
        "firm": _pack(firm),
        "by_horizon": {b: _pack(books[b]) for b in _BOOKS},
    }
