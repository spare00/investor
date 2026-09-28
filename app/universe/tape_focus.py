"""Committee names from the active watch. Python entry rules only — no LLM."""

from __future__ import annotations

from typing import Any

from app.agents.quant_strategist import (
    _liquidity,
    _momentum,
    _probability,
    _trend,
    _volatility,
)
from app.schemas.quant_strategist import BarSnapshot
from app.universe.book_strategy import (
    adjust_probability,
    apply_timing_probability,
    should_propose_entry,
)


def _bar(raw: Any) -> BarSnapshot | None:
    try:
        last = float(raw.last)
    except (TypeError, ValueError, AttributeError):
        return None
    if last <= 0:
        return None
    symbol = str(getattr(raw, "symbol", "") or "").upper().strip()
    if not symbol:
        return None

    def _opt(name: str) -> float | None:
        value = getattr(raw, name, None)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return BarSnapshot(
        symbol=symbol,
        last=last,
        open=_opt("open"),
        high=_opt("high"),
        low=_opt("low"),
        volume=_opt("volume"),
        avg_volume_20d=_opt("avg_volume_20d"),
        atr_14=_opt("atr_14"),
        rsi_14=_opt("rsi_14"),
        sma_20=_opt("sma_20"),
        sma_50=_opt("sma_50"),
        sma_200=_opt("sma_200"),
        bid=_opt("bid"),
        ask=_opt("ask"),
    )


def entry_score(
    raw: Any,
    *,
    horizon: str,
    regime: str | None = None,
    minutes_to_close: float | None = None,
) -> tuple[bool, float]:
    """Same tape rules Quant uses locally. False when the book would not enter."""
    bar = _bar(raw)
    if bar is None:
        return False, 0.0
    trend = _trend(bar, horizon)
    mom = _momentum(bar, horizon)
    vol = _volatility(bar, getattr(raw, "vix", None))
    liq = _liquidity(bar)
    base, _basis = _probability(trend, mom)
    prob, _notes = adjust_probability(
        base=base,
        horizon=horizon,
        liquidity=liq,
        volatility=vol,
        rsi=bar.rsi_14,
        volume=bar.volume,
        avg_volume=bar.avg_volume_20d,
    )
    prob, _notes, _timing = apply_timing_probability(
        prob,
        _notes,
        trend=trend,
        momentum=mom,
        rsi=bar.rsi_14,
        last=bar.last,
        open_=bar.open,
        high=bar.high,
        low=bar.low,
        sma_20=bar.sma_20,
    )
    ok = should_propose_entry(
        horizon=horizon,
        probability=prob,
        trend=trend,
        momentum=mom,
        liquidity=liq,
        volatility=vol,
        rsi=bar.rsi_14,
        regime=regime,
        volume=bar.volume,
        avg_volume=bar.avg_volume_20d,
        last=bar.last,
        open_=bar.open,
        high=bar.high,
        low=bar.low,
        sma_20=bar.sma_20,
        minutes_to_close=minutes_to_close,
    )
    return ok, prob


def select_setup_symbols(
    bars: list[Any],
    *,
    horizon_by_symbol: dict[str, str],
    holdings: list[str],
    limit: int,
    regime: str | None = None,
    minutes_to_close: float | None = None,
) -> list[str] | None:
    """Holdings, then entry-rule passers, capped at ``limit`` unless holdings exceed it.

    ``None`` means no usable bar — the caller should rotate inside the watch
    so the next cycle has tape. An empty list means the watch was scored and
    nothing passed.
    """
    held_set = {str(h).upper() for h in holdings if h}
    held: list[str] = []
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    any_bar = False
    for raw in bars:
        bar = _bar(raw)
        if bar is None or bar.symbol in seen:
            continue
        any_bar = True
        seen.add(bar.symbol)
        if bar.symbol in held_set:
            held.append(bar.symbol)
            continue
        horizon = horizon_by_symbol.get(bar.symbol) or "short"
        ok, prob = entry_score(
            raw,
            horizon=horizon,
            regime=regime,
            minutes_to_close=minutes_to_close,
        )
        if ok:
            scored.append((prob, bar.symbol))
    if not any_bar:
        return None
    for sym in holdings:
        name = str(sym).upper()
        if name and name not in held:
            held.append(name)
    scored.sort(key=lambda item: (-item[0], item[1]))
    cap = max(int(limit), len(held))
    picked = list(held)
    for _prob, sym in scored:
        if len(picked) >= cap:
            break
        if sym not in picked:
            picked.append(sym)
    return picked
