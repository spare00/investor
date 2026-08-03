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


def evaluate_decision(
    *,
    decision_price: float,
    action: DecisionAction | str,
    horizon_price: float | None,
    benchmark_return: float | None = None,
) -> dict[str, MetricResult | Any]:
    action = DecisionAction(action) if isinstance(action, str) else action
    if horizon_price is None or decision_price <= 0:
        return {
            "action": action.value,
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
        "realized_return": metric_result("realized_return", realized, method="decision_eval"),
        "vs_benchmark": vs_bench,
        "directional_correct": directional,
        "abstention_quality": abstention,
        "quality_score": metric_result("quality_score", score, method="decision_eval"),
    }
