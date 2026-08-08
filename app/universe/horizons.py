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
    # Stop / invalidation widths (longs: stop below reference)
    stop_atr_mult: float
    stop_pct_fallback: float
    min_stop_pct: float
    overnight_default: bool
    # Short book: overnight ok but event/gap → force review/flatten preference
    overnight_event_strict: bool
    news_lookback_minutes: int
    selection_notes: str
    stop_notes: str


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
        stop_atr_mult=1.0,
        stop_pct_fallback=0.01,
        min_stop_pct=0.005,
        overnight_default=False,
        overnight_event_strict=True,
        news_lookback_minutes=60,
        selection_notes=(
            "Ultra-liquid names only; tight spreads; avoid overnight gap risk; "
            "cut quickly on thesis break; no illiquid small caps."
        ),
        stop_notes="Tight structure/ATR stops (~1× ATR or ~1%); flatten on noise break.",
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
        stop_atr_mult=1.5,
        stop_pct_fallback=0.015,
        min_stop_pct=0.008,
        overnight_default=False,
        overnight_event_strict=True,
        news_lookback_minutes=90,
        selection_notes=(
            "Intraday catalysts, clean levels, enough volume to enter/exit; "
            "prefer flatten before close unless overnight thesis is explicit."
        ),
        stop_notes="Session structure stops (~1.5× ATR or ~1.5%); avoid holding through close.",
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
        stop_atr_mult=2.5,
        stop_pct_fallback=0.03,
        min_stop_pct=0.015,
        overnight_default=True,
        overnight_event_strict=True,
        news_lookback_minutes=180,
        selection_notes=(
            "Multi-day swings with defined invalidation; tolerate normal noise; "
            "revalidate on regime/news shifts."
        ),
        stop_notes=(
            "Wider swing stops (~2.5× ATR or ~3%); overnight ok but flatten/review "
            "on earnings, macro events, or elevated gap risk."
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
        stop_atr_mult=3.5,
        stop_pct_fallback=0.05,
        min_stop_pct=0.025,
        overnight_default=True,
        overnight_event_strict=False,
        news_lookback_minutes=360,
        selection_notes=(
            "Theme/regime aligned; wider stops vs noise; size smaller relative to "
            "volatility; review on weekly structure breaks, not tick noise."
        ),
        stop_notes=(
            "Position-style invalidation (~3.5× ATR or ~5%); overnight default; "
            "event risk → review/reduce, not automatic flatten."
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
                "stop_atr_mult": p.stop_atr_mult,
                "stop_pct_fallback": p.stop_pct_fallback,
                "overnight_default": p.overnight_default,
                "overnight_event_strict": p.overnight_event_strict,
                "news_lookback_minutes": p.news_lookback_minutes,
                "selection_notes": p.selection_notes,
                "stop_notes": p.stop_notes,
            }
        )
    return out


def enrich_watchlist_context(rows: list[dict] | None) -> list[dict]:
    """Attach policy fields so agent prompts can reason per book."""
    out: list[dict] = []
    for row in rows or []:
        sym = str(row.get("symbol") or "").upper()
        hz = row.get("horizon")
        if not sym or not hz:
            continue
        try:
            pol = policy_for(hz)
        except ValueError:
            out.append({"symbol": sym, "horizon": str(hz)})
            continue
        out.append(
            {
                "symbol": sym,
                "horizon": pol.horizon.value,
                "label_ko": pol.label_ko,
                "typical_hold": pol.typical_hold,
                "cio_time_horizon": pol.cio_time_horizon.value,
                "max_holding_minutes": pol.max_holding_minutes,
                "risk_per_trade_mult": pol.risk_per_trade_mult,
                "stop_atr_mult": pol.stop_atr_mult,
                "stop_pct_fallback": pol.stop_pct_fallback,
                "min_stop_pct": pol.min_stop_pct,
                "overnight_default": pol.overnight_default,
                "overnight_event_strict": pol.overnight_event_strict,
                "news_sensitive": pol.news_sensitive,
                "selection_notes": pol.selection_notes,
                "stop_notes": pol.stop_notes,
            }
        )
    return out


def policy_by_symbol(watchlist_context: list[dict] | None) -> dict[str, HorizonPolicy]:
    by_sym: dict[str, HorizonPolicy] = {}
    for row in watchlist_context or []:
        sym = str(row.get("symbol") or "").upper()
        hz = row.get("horizon")
        if not sym or not hz:
            continue
        try:
            by_sym[sym] = policy_for(hz)
        except ValueError:
            continue
    return by_sym


def overnight_allowed_for_horizon(horizon: UniverseHorizon | str | None) -> bool:
    if horizon is None:
        return False
    try:
        return bool(policy_for(horizon).overnight_default)
    except ValueError:
        return False


def closing_policy_for_horizon(horizon: UniverseHorizon | str | None) -> str:
    if overnight_allowed_for_horizon(horizon):
        try:
            if policy_for(horizon).overnight_event_strict:
                return "OVERNIGHT_WITH_EVENT_REVIEW"
        except ValueError:
            pass
        return "ALLOW_OVERNIGHT"
    return "CLOSE_INTRADAY_ONLY"


def news_lookback_minutes_for_symbols(
    horizon_by_symbol: dict[str, str] | None,
    *,
    default_minutes: int = 90,
) -> int:
    """Use the longest book lookback among symbols under review."""
    if not horizon_by_symbol:
        return default_minutes
    mins = [default_minutes]
    for hz in horizon_by_symbol.values():
        try:
            mins.append(int(policy_for(hz).news_lookback_minutes))
        except ValueError:
            continue
    return max(mins)


def suggested_long_stop(
    *,
    reference: float,
    atr: float | None = None,
    policy: HorizonPolicy | None = None,
) -> float | None:
    """Compute a long stop below reference using ATR mult, else pct fallback."""
    if reference <= 0:
        return None
    pol = policy
    if atr is not None and atr > 0 and pol is not None:
        stop = reference - float(pol.stop_atr_mult) * float(atr)
    elif atr is not None and atr > 0:
        stop = reference - 1.5 * float(atr)
    elif pol is not None:
        stop = reference * (1.0 - float(pol.stop_pct_fallback))
    else:
        stop = reference * 0.98
    if pol is not None:
        floor = reference * (1.0 - float(pol.min_stop_pct))
        # Stop must be at or below floor (wider = lower for longs)
        stop = min(stop, floor)
    if stop <= 0 or stop >= reference:
        return None
    return round(stop, 4)


def widen_long_stop_if_too_tight(
    *,
    stop: float,
    reference: float,
    policy: HorizonPolicy,
) -> float:
    """Ensure stop distance is at least min_stop_pct for the book."""
    if reference <= 0 or stop <= 0:
        return stop
    floor = reference * (1.0 - float(policy.min_stop_pct))
    if stop > floor:
        return round(floor, 4)
    return stop


def align_cio_horizons(
    decision: "CIODecision",
    watchlist_context: list[dict] | None,
) -> "CIODecision":
    """Stamp CIO symbol plans with watchlist horizon → cio_time_horizon / max hold."""
    from app.schemas.cio import CIODecision, SymbolActionPlan

    if not watchlist_context:
        return decision
    by_sym = policy_by_symbol(watchlist_context)
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
