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
    "NDQ": "short",
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
    target_size_pct: float
    require_uptrend: bool
    allow_sideways_momentum: bool
    require_accelerating: bool
    max_rsi: float
    min_rsi: float
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
            "Tape follow: ultra-liquid names only, tight stop, no overnight. "
            "Enter continuation (up + accelerating), skip RSI exhaustion and wide spreads. "
            "Cut on noise break — do not average down."
        ),
        min_probability=0.58,
        entry_zone_pct=0.0015,
        target_pct=0.008,
        target_size_pct=5.0,
        require_uptrend=False,
        allow_sideways_momentum=True,
        require_accelerating=True,
        max_rsi=68.0,
        min_rsi=52.0,
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
            "Session structure: trade the day, flatten before close. "
            "Enter aligned trend without exhaustion; 1.5× ATR invalidation. "
            "Do not hold through the close or overnight."
        ),
        min_probability=0.58,
        entry_zone_pct=0.003,
        target_pct=0.015,
        target_size_pct=8.0,
        require_uptrend=True,
        allow_sideways_momentum=True,
        require_accelerating=False,
        max_rsi=70.0,
        min_rsi=48.0,
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
            "Swing: multi-session trend with wider stops, overnight allowed. "
            "Enter SMA-aligned uptrends; tolerate noise. Reduce on exhaustion, "
            "exit only if the swing trend actually breaks."
        ),
        min_probability=0.55,
        entry_zone_pct=0.008,
        target_pct=0.03,
        target_size_pct=10.0,
        require_uptrend=True,
        allow_sideways_momentum=True,
        require_accelerating=False,
        max_rsi=75.0,
        min_rsi=45.0,
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


def adjust_probability(
    *,
    base: float,
    horizon: str,
    liquidity: LiquidityState,
    volatility: VolatilityState,
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
    return max(0.05, min(0.95, round(score, 2))), notes


def structure_allows_entry(
    *,
    horizon: str,
    trend: TrendState,
    momentum: MomentumState,
    liquidity: LiquidityState,
    volatility: VolatilityState,
    rsi: float | None,
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
    if rsi is not None:
        if rsi > book.max_rsi:
            return False, f"rsi_high_{rsi:.0f}"
        if rsi < book.min_rsi:
            return False, f"rsi_low_{rsi:.0f}"
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


def policy_time_horizon(horizon: str | None) -> TimeHorizon:
    book = playbook_for(horizon)
    if book is not None:
        return book.cio_time_horizon
    try:
        return policy_for(horizon or "short").cio_time_horizon
    except ValueError:
        return TimeHorizon.INTRADAY
