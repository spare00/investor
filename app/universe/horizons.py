"""Trade-horizon groups for AI-managed universe selection.

초단타 / 단타 / 단기 / 중기 — each group has distinct holding, re-eval, and risk traits.
The goal is asymmetric outcomes: pursue return inside the style, cut loss by style rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.schemas.common import TimeHorizon


class UniverseHorizon(StrEnum):
    """Watchlist / focus grouping (Korean ops labels in docs)."""

    SCALP = "scalp"  # 초단타 — minutes to hours
    DAY = "day"  # 단타 — same session / overnight max
    SHORT = "short"  # 단기 — days to ~2 weeks
    MEDIUM = "medium"  # 중기 — weeks to a few months


@dataclass(frozen=True, slots=True)
class HorizonPolicy:
    horizon: UniverseHorizon
    label_ko: str
    label_en: str
    typical_hold: str
    max_holding_minutes: int | None
    reeval_seconds: int
    risk_per_trade_mult: float
    max_positions: int
    min_avg_daily_volume: float
    max_spread_bps: float
    news_sensitive: bool
    prefer_liquid_etf_or_mega: bool
    cio_time_horizon: TimeHorizon
    selection_notes: str


HORIZON_POLICIES: dict[UniverseHorizon, HorizonPolicy] = {
    UniverseHorizon.SCALP: HorizonPolicy(
        horizon=UniverseHorizon.SCALP,
        label_ko="초단타",
        label_en="scalp",
        typical_hold="minutes–hours",
        max_holding_minutes=240,
        reeval_seconds=120,
        risk_per_trade_mult=0.5,
        max_positions=2,
        min_avg_daily_volume=5_000_000,
        max_spread_bps=15,
        news_sensitive=True,
        prefer_liquid_etf_or_mega=True,
        cio_time_horizon=TimeHorizon.INTRADAY,
        selection_notes=(
            "Ultra-liquid names only; tight spreads; avoid overnight gap risk; "
            "cut quickly on thesis break; no illiquid small caps."
        ),
    ),
    UniverseHorizon.DAY: HorizonPolicy(
        horizon=UniverseHorizon.DAY,
        label_ko="단타",
        label_en="day",
        typical_hold="same session (flatten near close preferred)",
        max_holding_minutes=390,
        reeval_seconds=300,
        risk_per_trade_mult=0.75,
        max_positions=3,
        min_avg_daily_volume=2_000_000,
        max_spread_bps=25,
        news_sensitive=True,
        prefer_liquid_etf_or_mega=True,
        cio_time_horizon=TimeHorizon.INTRADAY,
        selection_notes=(
            "Intraday catalysts, clean levels, enough volume to enter/exit; "
            "prefer flatten before close unless overnight thesis is explicit."
        ),
    ),
    UniverseHorizon.SHORT: HorizonPolicy(
        horizon=UniverseHorizon.SHORT,
        label_ko="단기",
        label_en="short",
        typical_hold="2–10 sessions",
        max_holding_minutes=10 * 24 * 60,
        reeval_seconds=900,
        risk_per_trade_mult=1.0,
        max_positions=4,
        min_avg_daily_volume=1_000_000,
        max_spread_bps=40,
        news_sensitive=True,
        prefer_liquid_etf_or_mega=False,
        cio_time_horizon=TimeHorizon.SWING,
        selection_notes=(
            "Multi-day swings with defined invalidation; tolerate normal noise; "
            "revalidate on regime/news shifts."
        ),
    ),
    UniverseHorizon.MEDIUM: HorizonPolicy(
        horizon=UniverseHorizon.MEDIUM,
        label_ko="중기",
        label_en="medium",
        typical_hold="2–12 weeks",
        max_holding_minutes=60 * 24 * 60,
        reeval_seconds=3600,
        risk_per_trade_mult=1.0,
        max_positions=5,
        min_avg_daily_volume=500_000,
        max_spread_bps=50,
        news_sensitive=False,
        prefer_liquid_etf_or_mega=False,
        cio_time_horizon=TimeHorizon.POSITION,
        selection_notes=(
            "Theme/regime aligned; wider stops vs noise; size smaller relative to "
            "volatility; review on weekly structure breaks, not tick noise."
        ),
    ),
}


def policy_for(horizon: UniverseHorizon | str) -> HorizonPolicy:
    return HORIZON_POLICIES[UniverseHorizon(horizon)]


def all_horizon_summaries() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for h, p in HORIZON_POLICIES.items():
        out.append(
            {
                "horizon": h.value,
                "label_ko": p.label_ko,
                "label_en": p.label_en,
                "typical_hold": p.typical_hold,
                "max_positions": p.max_positions,
                "reeval_seconds": p.reeval_seconds,
                "risk_per_trade_mult": p.risk_per_trade_mult,
                "selection_notes": p.selection_notes,
            }
        )
    return out


def align_cio_horizons(
    decision: "CIODecision",
    watchlist_context: list[dict] | None,
) -> "CIODecision":
    """Stamp CIO symbol plans with watchlist horizon → cio_time_horizon / max hold."""
    from app.schemas.cio import CIODecision, SymbolActionPlan

    if not watchlist_context:
        return decision
    by_sym: dict[str, HorizonPolicy] = {}
    for row in watchlist_context:
        sym = str(row.get("symbol") or "").upper()
        hz = row.get("horizon")
        if not sym or not hz:
            continue
        try:
            by_sym[sym] = policy_for(hz)
        except ValueError:
            continue
    if not by_sym:
        return decision
    updated: list[SymbolActionPlan] = []
    changed = False
    for plan in decision.symbol_actions:
        pol = by_sym.get(plan.symbol.upper())
        if pol is None:
            updated.append(plan)
            continue
        kwargs: dict = {}
        if plan.time_horizon != pol.cio_time_horizon:
            kwargs["time_horizon"] = pol.cio_time_horizon
        if pol.max_holding_minutes and plan.max_holding_time_minutes is None:
            kwargs["max_holding_time_minutes"] = pol.max_holding_minutes
        if kwargs:
            changed = True
            updated.append(plan.model_copy(update=kwargs))
        else:
            updated.append(plan)
    if not changed:
        return decision
    return decision.model_copy(update={"symbol_actions": updated})
