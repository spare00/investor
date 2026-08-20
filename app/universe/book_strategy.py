"""Per-horizon trading playbooks.

Stops, caps, and overnight policy already live on HorizonPolicy.
This module is the missing piece: Quant entry/exit *rules* and CIO action
choice so scalp / day / short are not one generic 2% continuation model.

Medium is held if already open, but is not a research or entry book.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from app.schemas.common import (
    LiquidityState,
    MarketRegime,
    MomentumState,
    PortfolioAction,
    SymbolAction,
    TimeHorizon,
    TrendState,
    VolatilityState,
)
from app.universe.horizons import UniverseHorizon, policy_for


ACTIVE_STRATEGY_HORIZONS: frozenset[str] = frozenset(
    {
        UniverseHorizon.SCALP.value,
        UniverseHorizon.DAY.value,
        UniverseHorizon.SHORT.value,
    }
)

# Used when agents get bars before watchlist context is attached.
_DEFAULT_HORIZON: dict[str, str] = {
    "SPY": "scalp",
    "QQQ": "scalp",
    "IWM": "day",
    "DIA": "day",
    "VAS": "day",
    "NDQ": "scalp",
    "IOZ": "short",
    "BHP": "short",
    "CBA": "short",
}


class BookExit(StrEnum):
    HOLD = "hold"
    REDUCE = "reduce"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class BookPlaybook:
    horizon: str
    label_ko: str
    summary: str
    min_probability: float
    entry_zone_pct: float
    target_pct: float
    # Concentration cap (notional % of equity). Actual size = risk_budget / stop.
    target_size_pct: float
    # Capital-at-risk target, % of equity. Shares = budget / stop_distance.
    risk_budget_pct: float
    require_uptrend: bool
    allow_sideways_momentum: bool
    require_accelerating: bool
    require_volume_accel: bool
    volume_accel_mult: float
    require_short_ma: bool
    require_session_structure: bool
    prefer_rsi_min: float
    prefer_rsi_max: float
    rsi_hard_min: float | None
    rsi_hard_max: float | None
    reject_liquidity: frozenset[LiquidityState]
    sell_if_exhausted: bool
    sell_if_liquidity_stressed: bool
    sell_if_downtrend: bool
    reduce_if_exhausted: bool
    new_only_regimes: frozenset[MarketRegime]
    max_new_per_cycle: int
    cio_time_horizon: TimeHorizon


PLAYBOOKS: dict[str, BookPlaybook] = {
    "scalp": BookPlaybook(
        horizon="scalp",
        label_ko="초단타",
        summary=(
            "Tape: price + volume acceleration, tight spread, last above sma20. "
            "RSI is a haircut not a gate. Tight stop, no overnight. Cut on noise — no average-down."
        ),
        min_probability=0.58,
        entry_zone_pct=0.0015,
        target_pct=0.008,
        target_size_pct=8.0,
        risk_budget_pct=0.15,
        require_uptrend=False,
        allow_sideways_momentum=True,
        require_accelerating=True,
        require_volume_accel=True,
        volume_accel_mult=1.15,
        require_short_ma=True,
        require_session_structure=False,
        prefer_rsi_min=52.0,
        prefer_rsi_max=68.0,
        rsi_hard_min=None,
        rsi_hard_max=85.0,
        reject_liquidity=frozenset({LiquidityState.TIGHT, LiquidityState.STRESSED}),
        sell_if_exhausted=True,
        sell_if_liquidity_stressed=True,
        sell_if_downtrend=True,
        reduce_if_exhausted=False,
        new_only_regimes=frozenset({MarketRegime.RISK_ON, MarketRegime.STRONG_RISK_ON}),
        max_new_per_cycle=1,
        cio_time_horizon=TimeHorizon.INTRADAY,
    ),
    "day": BookPlaybook(
        horizon="day",
        label_ko="단타",
        summary=(
            "Session structure: last holds above typical price and the open. "
            "Not a tape-acceleration trade. Flatten before close. 1.5× ATR invalidation."
        ),
        min_probability=0.58,
        entry_zone_pct=0.003,
        target_pct=0.015,
        target_size_pct=10.0,
        risk_budget_pct=0.15,
        require_uptrend=True,
        allow_sideways_momentum=False,
        require_accelerating=False,
        require_volume_accel=False,
        volume_accel_mult=1.0,
        require_short_ma=False,
        require_session_structure=True,
        prefer_rsi_min=45.0,
        prefer_rsi_max=70.0,
        rsi_hard_min=None,
        rsi_hard_max=85.0,
        reject_liquidity=frozenset({LiquidityState.STRESSED}),
        sell_if_exhausted=True,
        sell_if_liquidity_stressed=True,
        sell_if_downtrend=True,
        reduce_if_exhausted=False,
        new_only_regimes=frozenset({MarketRegime.RISK_ON, MarketRegime.STRONG_RISK_ON}),
        max_new_per_cycle=1,
        cio_time_horizon=TimeHorizon.INTRADAY,
    ),
    "short": BookPlaybook(
        horizon="short",
        label_ko="단기",
        summary=(
            "Swing: SMA50/200 aligned uptrend; tolerate noise. Reduce on exhaustion, "
            "sell only if the swing trend actually breaks. Overnight ok. Size from risk budget."
        ),
        min_probability=0.55,
        entry_zone_pct=0.008,
        target_pct=0.03,
        target_size_pct=10.0,
        risk_budget_pct=0.15,
        require_uptrend=True,
        allow_sideways_momentum=True,
        require_accelerating=False,
        require_volume_accel=False,
        volume_accel_mult=1.0,
        require_short_ma=False,
        require_session_structure=False,
        prefer_rsi_min=45.0,
        prefer_rsi_max=75.0,
        rsi_hard_min=None,
        rsi_hard_max=None,
        reject_liquidity=frozenset({LiquidityState.STRESSED}),
        sell_if_exhausted=False,
        sell_if_liquidity_stressed=False,
        sell_if_downtrend=True,
        reduce_if_exhausted=True,
        new_only_regimes=frozenset(
            {MarketRegime.RISK_ON, MarketRegime.STRONG_RISK_ON, MarketRegime.NEUTRAL}
        ),
        max_new_per_cycle=1,
        cio_time_horizon=TimeHorizon.SWING,
    ),
}


def is_active_strategy_horizon(horizon: str | None) -> bool:
    return str(horizon or "").strip().lower() in ACTIVE_STRATEGY_HORIZONS


def playbook_for(horizon: str | None) -> BookPlaybook | None:
    key = str(horizon or "").strip().lower()
    return PLAYBOOKS.get(key)


def filter_strategy_horizons(horizons: Iterable[str | None]) -> list[str]:
    """Drop medium / unknown so cadence and focus follow tradable books."""
    out: list[str] = []
    for raw in horizons:
        key = str(raw or "").strip().lower()
        if key in ACTIVE_STRATEGY_HORIZONS and key not in out:
            out.append(key)
    return out


def horizon_for_symbol(symbol: str, watchlist: list[dict] | None = None) -> str:
    sym = str(symbol or "").upper()
    for row in watchlist or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").upper() != sym:
            continue
        hz = str(row.get("horizon") or "").strip().lower()
        if hz:
            return hz
    return _DEFAULT_HORIZON.get(sym, UniverseHorizon.SHORT.value)


def playbook_cards() -> list[dict[str, str]]:
    """Compact cards for LLM briefs — one strategy per book."""
    return [
        {"h": p.horizon, "ko": p.label_ko, "rule": p.summary}
        for p in PLAYBOOKS.values()
    ]


def risk_mult_for_horizon(horizon: str | None, *, firm_risk_pct: float) -> float:
    """Map playbook capital-at-risk onto Settings.risk_per_trade_pct."""
    book = playbook_for(horizon)
    if firm_risk_pct <= 0:
        return 1.0
    if book is not None:
        return float(book.risk_budget_pct) / float(firm_risk_pct)
    try:
        return float(policy_for(horizon or "short").risk_per_trade_mult)
    except ValueError:
        return 1.0


def notional_pct_for_risk(
    *,
    horizon: str | None,
    entry: float,
    stop: float,
    max_position_pct: float,
) -> float:
    """Invert stop distance into a notional weight, then apply book/firm caps."""
    book = playbook_for(horizon)
    budget = float(book.risk_budget_pct) if book else 0.15
    cap = float(max_position_pct)
    if book is not None:
        cap = min(cap, float(book.target_size_pct))
    if entry <= 0:
        return cap
    stop_frac = abs(float(entry) - float(stop)) / float(entry)
    if stop_frac <= 1e-9:
        return cap
    raw = (budget / 100.0) / stop_frac * 100.0
    return round(min(cap, max(0.0, raw)), 2)


def adjust_probability(
    *,
    base: float,
    horizon: str,
    liquidity: LiquidityState,
    volatility: VolatilityState,
    rsi: float | None = None,
    volume: float | None = None,
    avg_volume: float | None = None,
) -> tuple[float, list[str]]:
    score = float(base)
    notes = [f"book={horizon}"]
    book = playbook_for(horizon)
    if book is None:
        score = min(score, 0.35)
        notes.append("medium_ignored=-cap")
        return max(0.05, min(0.95, round(score, 2))), notes
    if liquidity in book.reject_liquidity:
        score -= 0.15
        notes.append("liq_reject=-0.15")
    if volatility == VolatilityState.EXTREME:
        score -= 0.15
        notes.append("vol_extreme=-0.15")
    elif volatility == VolatilityState.ELEVATED and horizon == "scalp":
        score -= 0.08
        notes.append("vol_elev_scalp=-0.08")
    if rsi is not None:
        if rsi > book.prefer_rsi_max:
            score -= 0.05
            notes.append("rsi_hot=-0.05")
        elif rsi < book.prefer_rsi_min:
            score -= 0.05
            notes.append("rsi_cool=-0.05")
    if (
        book.require_volume_accel
        and volume is not None
        and avg_volume is not None
        and avg_volume > 0
        and volume >= book.volume_accel_mult * avg_volume
    ):
        score += 0.05
        notes.append("vol_accel=+0.05")
    return max(0.05, min(0.95, round(score, 2))), notes


def structure_allows_entry(
    *,
    horizon: str,
    trend: TrendState,
    momentum: MomentumState,
    liquidity: LiquidityState,
    volatility: VolatilityState,
    rsi: float | None,
    volume: float | None = None,
    avg_volume: float | None = None,
    last: float | None = None,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    sma_20: float | None = None,
) -> tuple[bool, str]:
    book = playbook_for(horizon)
    if book is None:
        return False, "medium_book_ignored"
    if liquidity in book.reject_liquidity:
        return False, f"liquidity_{liquidity.value}"
    if volatility == VolatilityState.EXTREME:
        return False, "vol_extreme"
    up = trend in {TrendState.UP, TrendState.STRONG_UP}
    down = trend in {TrendState.DOWN, TrendState.STRONG_DOWN}
    if down:
        return False, "trend_down"
    if book.require_uptrend and not up:
        if not (
            book.allow_sideways_momentum
            and trend == TrendState.SIDEWAYS
            and momentum == MomentumState.ACCELERATING
        ):
            return False, f"trend_{trend.value}"
    if book.require_accelerating and momentum != MomentumState.ACCELERATING:
        return False, f"mom_{momentum.value}"
    if momentum == MomentumState.EXHAUSTED and book.sell_if_exhausted:
        return False, "exhausted"
    if (
        book.require_volume_accel
        and volume is not None
        and avg_volume is not None
        and avg_volume > 0
        and volume < book.volume_accel_mult * avg_volume
    ):
        return False, "volume_flat"
    if book.require_short_ma and sma_20 is not None and last is not None and last <= sma_20:
        return False, "below_sma20"
    if book.require_session_structure and last is not None:
        if open_ is not None and last < open_:
            return False, "below_open"
        if high is not None and low is not None and high > low:
            typical = (float(high) + float(low) + float(last)) / 3.0
            if last < typical:
                return False, "below_session_vwap"
    if rsi is not None:
        if book.rsi_hard_max is not None and rsi > book.rsi_hard_max:
            return False, f"rsi_extreme_{rsi:.0f}"
        if book.rsi_hard_min is not None and rsi < book.rsi_hard_min:
            return False, f"rsi_extreme_{rsi:.0f}"
    return True, "ok"


def should_propose_entry(
    *,
    horizon: str,
    probability: float,
    trend: TrendState,
    momentum: MomentumState,
    liquidity: LiquidityState,
    volatility: VolatilityState,
    rsi: float | None,
    regime: str | MarketRegime | None = None,
    volume: float | None = None,
    avg_volume: float | None = None,
    last: float | None = None,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    sma_20: float | None = None,
) -> bool:
    book = playbook_for(horizon)
    if book is None:
        return False
    if probability < book.min_probability:
        return False
    ok, _ = structure_allows_entry(
        horizon=horizon,
        trend=trend,
        momentum=momentum,
        liquidity=liquidity,
        volatility=volatility,
        rsi=rsi,
        volume=volume,
        avg_volume=avg_volume,
        last=last,
        open_=open_,
        high=high,
        low=low,
        sma_20=sma_20,
    )
    if not ok:
        return False
    if regime is not None:
        try:
            reg = regime if isinstance(regime, MarketRegime) else MarketRegime(str(regime))
        except ValueError:
            reg = None
        if reg is not None and reg not in book.new_only_regimes:
            return False
    return True


def exit_action(
    *,
    horizon: str,
    trend: TrendState,
    momentum: MomentumState,
    liquidity: LiquidityState,
) -> BookExit:
    book = playbook_for(horizon)
    if book is None:
        return BookExit.HOLD
    down = trend in {TrendState.DOWN, TrendState.STRONG_DOWN}
    if book.sell_if_downtrend and down:
        return BookExit.SELL
    if book.sell_if_liquidity_stressed and liquidity == LiquidityState.STRESSED:
        return BookExit.SELL
    if book.sell_if_exhausted and momentum == MomentumState.EXHAUSTED:
        return BookExit.SELL
    if book.reduce_if_exhausted and momentum == MomentumState.EXHAUSTED:
        return BookExit.REDUCE
    return BookExit.HOLD


def symbol_action_for_exit(decision: BookExit) -> SymbolAction:
    if decision == BookExit.SELL:
        return SymbolAction.SELL
    if decision == BookExit.REDUCE:
        return SymbolAction.REDUCE
    return SymbolAction.HOLD


def portfolio_action_from_symbol_actions(
    actions: Iterable[Any],
) -> PortfolioAction:
    def _kind(item: Any) -> Any:
        if isinstance(item, dict):
            return item.get("action", item)
        return getattr(item, "action", item)

    kinds = {_kind(a) for a in actions}
    values = {
        (k.value if hasattr(k, "value") else str(k)) for k in kinds
    }
    if values & {"BUY", "STRONG_BUY", "SCALE_IN", "ADD"}:
        return PortfolioAction.SCALE_IN
    if values & {"SELL", "PARTIAL_SELL"}:
        return PortfolioAction.REDUCE
    if "REDUCE" in values:
        return PortfolioAction.REDUCE
    if values:
        return PortfolioAction.HOLD
    return PortfolioAction.NO_TRADE


def align_cio_playbook_exits(
    decision: Any,
    quant: Any,
    watchlist: list[dict] | None,
    *,
    held_symbols: Iterable[str] | None = None,
) -> Any:
    """Rewrite LLM exits that violate the book (e.g. REDUCE a quiet short)."""
    if decision is None:
        return decision
    held = {str(s).upper() for s in (held_symbols or []) if s}
    views = {
        str(v.symbol).upper(): v
        for v in (getattr(quant, "symbol_views", None) or [])
        if getattr(v, "symbol", None)
    }
    updated = []
    changed = False
    for plan in decision.symbol_actions:
        action = plan.action
        if action not in {SymbolAction.SELL, SymbolAction.PARTIAL_SELL, SymbolAction.REDUCE}:
            updated.append(plan)
            continue
        sym = str(plan.symbol or "").upper()
        if held and sym not in held:
            updated.append(plan)
            continue
        hz = horizon_for_symbol(sym, watchlist)
        view = views.get(sym)
        if view is None:
            if hz == "short" and action in {SymbolAction.REDUCE, SymbolAction.PARTIAL_SELL}:
                changed = True
                updated.append(
                    plan.model_copy(
                        update={
                            "action": SymbolAction.HOLD,
                            "thesis": f"{hz}: hold — no tape to break swing",
                        }
                    )
                )
                continue
            updated.append(plan)
            continue
        allowed = exit_action(
            horizon=hz,
            trend=view.trend_state,
            momentum=view.momentum_state,
            liquidity=view.liquidity_state,
        )
        want = symbol_action_for_exit(allowed)
        if want == SymbolAction.HOLD:
            changed = True
            updated.append(
                plan.model_copy(
                    update={
                        "action": SymbolAction.HOLD,
                        "thesis": f"{hz}: {allowed.value} — keep swing",
                    }
                )
            )
            continue
        if want == SymbolAction.REDUCE and action == SymbolAction.SELL:
            changed = True
            updated.append(plan.model_copy(update={"action": SymbolAction.REDUCE}))
            continue
        if (
            want == SymbolAction.SELL
            and action in {SymbolAction.REDUCE, SymbolAction.PARTIAL_SELL}
            and hz in {"scalp", "day"}
        ):
            changed = True
            updated.append(plan.model_copy(update={"action": SymbolAction.SELL}))
            continue
        updated.append(plan)
    if not changed:
        return decision
    portfolio = portfolio_action_from_symbol_actions(updated)
    return decision.model_copy(
        update={"symbol_actions": updated, "portfolio_action": portfolio}
    )


def policy_time_horizon(horizon: str | None) -> TimeHorizon:
    book = playbook_for(horizon)
    if book is not None:
        return book.cio_time_horizon
    try:
        return policy_for(horizon or "short").cio_time_horizon
    except ValueError:
        return TimeHorizon.INTRADAY
