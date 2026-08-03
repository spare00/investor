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
