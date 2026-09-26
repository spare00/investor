"""Expectancy by entry reason / named policy cohorts.

Buckets are independent gates (a trade may appear in more than one).
"""

from __future__ import annotations

import statistics
from typing import Any

from app.performance.trades import ClosedTrade
from app.universe.entry_attribution import (
    COHORT_INJECTED_SIDEWAYS,
    COHORT_SCALP_FLATTEN,
    COHORT_SHORT_BOUNCE,
    ENTRY_REASONS,
    EXIT_GIVEBACK,
    EXIT_SESSION_FLATTEN,
    EXIT_STOP,
    EXIT_TAKE_PROFIT,
    SOURCE_INJECTED,
    public_entry_reason,
)

# Settlement stamps fees=0. 8 bps round-trip is a conservative liquid-name
# paper cost (≈4 bps each way) so expectancy is not gross-only.
DEFAULT_ROUND_TRIP_COST_BPS = 8.0

COST_NOTE = (
    "expectancy_after_costs = pnl - modeled round-trip cost "
    f"({DEFAULT_ROUND_TRIP_COST_BPS:.0f} bps of entry notional). "
    "Broker fees are not populated on lifecycle closes."
)


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _rate(hits: int, n: int) -> float | None:
    if n <= 0:
        return None
    return hits / n


def modeled_cost(trade: ClosedTrade, *, bps: float = DEFAULT_ROUND_TRIP_COST_BPS) -> float:
    if trade.fees and trade.fees > 0:
        return float(trade.fees)
    notional = float(trade.notional or 0.0)
    if notional <= 0:
        return 0.0
    return notional * float(bps) / 10_000.0


def empty_gate_row() -> dict[str, Any]:
    return {
        "n": 0,
        "win_pct": None,
        "avg_mfe": None,
        "avg_mae": None,
        "tp_pct": None,
        "stop_pct": None,
        "giveback_exit_pct": None,
        "session_flatten_pct": None,
        "expectancy_after_costs": None,
    }


def gate_expectancy(trades: list[ClosedTrade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return empty_gate_row()
    nets = [t.pnl - modeled_cost(t) for t in trades]
    wins = sum(1 for p in nets if p > 0)
    mfe = [t.mfe_pct for t in trades if t.mfe_pct is not None]
    mae = [t.mae_pct for t in trades if t.mae_pct is not None]
    exits = [str(t.exit_reason or "") for t in trades]
    return {
        "n": n,
        "win_pct": _rate(wins, n),
        "avg_mfe": _mean(mfe),
        "avg_mae": _mean(mae),
        "tp_pct": _rate(sum(1 for e in exits if e == EXIT_TAKE_PROFIT), n),
        "stop_pct": _rate(sum(1 for e in exits if e == EXIT_STOP), n),
        "giveback_exit_pct": _rate(sum(1 for e in exits if e == EXIT_GIVEBACK), n),
        "session_flatten_pct": _rate(sum(1 for e in exits if e == EXIT_SESSION_FLATTEN), n),
        "expectancy_after_costs": _mean(nets),
    }


def _matches_reason(trade: ClosedTrade, reason: str) -> bool:
    if reason == "aggressive_injected":
        return str(trade.entry_source or "") == SOURCE_INJECTED
    public = public_entry_reason(trade.entry_timing)
    return public == reason


def _is_sideways(trade: ClosedTrade) -> bool:
    return str(trade.trend_at_entry or "").upper() in {"SIDEWAYS"}


def _in_cohort(trade: ClosedTrade, cohort: str) -> bool:
    hz = str(trade.horizon or "").lower()
    if cohort == COHORT_INJECTED_SIDEWAYS:
        return str(trade.entry_source or "") == SOURCE_INJECTED and _is_sideways(trade)
    if cohort == COHORT_SHORT_BOUNCE:
        return hz == "short" and public_entry_reason(trade.entry_timing) == "oversold_bounce"
    if cohort == COHORT_SCALP_FLATTEN:
        return hz == "scalp" and str(trade.exit_reason or "") == EXIT_SESSION_FLATTEN
    return False


def compute_entry_reason_expectancy(trades: list[ClosedTrade]) -> dict[str, Any]:
    tagged = [
        t
        for t in trades
        if t.entry_timing or t.entry_source or t.exit_reason or t.mfe_pct is not None
    ]
    by_reason = {
        reason: gate_expectancy([t for t in trades if _matches_reason(t, reason)])
        for reason in ENTRY_REASONS
    }
    cohorts = {
        COHORT_INJECTED_SIDEWAYS: gate_expectancy(
            [t for t in trades if _in_cohort(t, COHORT_INJECTED_SIDEWAYS)]
        ),
        COHORT_SHORT_BOUNCE: gate_expectancy(
            [t for t in trades if _in_cohort(t, COHORT_SHORT_BOUNCE)]
        ),
        COHORT_SCALP_FLATTEN: gate_expectancy(
            [t for t in trades if _in_cohort(t, COHORT_SCALP_FLATTEN)]
        ),
    }
    untagged = sum(
        1
        for t in trades
        if not t.entry_timing and str(t.entry_source or "") not in {SOURCE_INJECTED}
    )
    return {
        "by_entry_reason": by_reason,
        "cohorts": cohorts,
        "untagged": untagged,
        "tagged": len(trades) - untagged,
        "cost_bps_round_trip": DEFAULT_ROUND_TRIP_COST_BPS,
        "cost_note": COST_NOTE,
        "coverage_note": (
            "Pre-tag closes land in untagged. Next paper run stamps entry_timing / "
            "entry_source / trend_at_entry / exit_reason / peak+trough on the lifecycle."
        ),
        "trade_count_scored": len(tagged),
    }
