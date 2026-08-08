"""Recent closed-trade outcomes for universe feedback (observational only).

Does not auto-mutate strategy, risk limits, or prompts — supplies stats for
Universe Manager context and watchlist payload stamps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PositionLifecycle, WatchlistSymbol
from app.universe.horizons import UniverseHorizon


def _horizon_from_lifecycle(lc: PositionLifecycle, watchlist_hz: dict[str, str]) -> str:
    policy = dict(lc.exit_policy or {})
    raw = policy.get("horizon")
    if raw:
        try:
            return UniverseHorizon(str(raw).lower()).value
        except ValueError:
            pass
    return watchlist_hz.get(str(lc.symbol).upper(), "unknown")


async def recent_outcome_stats(
    session: AsyncSession,
    *,
    lookback_days: int = 90,
    now: datetime | None = None,
    min_trades_for_signal: int = 3,
) -> dict[str, Any]:
    """Aggregate CLOSED lifecycles by symbol and horizon book."""
    end = now or datetime.now(UTC)
    start = end - timedelta(days=max(1, int(lookback_days)))

    wl_rows = list((await session.execute(select(WatchlistSymbol))).scalars().all())
    watchlist_hz = {r.symbol.upper(): str(r.horizon) for r in wl_rows if r.symbol}
    source_by_sym = {r.symbol.upper(): str(r.source or "") for r in wl_rows if r.symbol}

    rows = list((await session.execute(select(PositionLifecycle))).scalars().all())
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_horizon: dict[str, list[float]] = defaultdict(list)
    symbol_horizon: dict[str, str] = {}

    for lc in rows:
        if lc.status != "CLOSED" or not lc.closed_at:
            continue
        closed = lc.closed_at if lc.closed_at.tzinfo else lc.closed_at.replace(tzinfo=UTC)
        if not (start <= closed <= end):
            continue
        sym = str(lc.symbol).upper()
        hz = _horizon_from_lifecycle(lc, watchlist_hz)
        pnl = float(lc.realized_pl or 0.0)
        by_symbol[sym].append(pnl)
        by_horizon[hz].append(pnl)
        symbol_horizon[sym] = hz

    def _pack(pnls: list[float]) -> dict[str, Any]:
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        expectancy = sum(pnls) / n if n else None
        return {
            "trade_count": n,
            "win_rate": (wins / n) if n else None,
            "expectancy": expectancy,
            "total_pnl": sum(pnls) if n else 0.0,
        }

    symbols_out: list[dict[str, Any]] = []
    for sym, pnls in sorted(by_symbol.items()):
        pack = _pack(pnls)
        pack["symbol"] = sym
        pack["horizon"] = symbol_horizon.get(sym, "unknown")
        pack["source"] = source_by_sym.get(sym) or None
        n = pack["trade_count"]
        exp = pack["expectancy"]
        signal = "insufficient"
        if n >= min_trades_for_signal and exp is not None:
            if exp > 0:
                signal = "positive"
            elif exp < 0:
                signal = "negative"
            else:
                signal = "flat"
        pack["signal"] = signal
        symbols_out.append(pack)

    horizons_out = {
        hz: _pack(pnls)
        for hz, pnls in sorted(by_horizon.items())
    }

    by_source_pnls: dict[str, list[float]] = defaultdict(list)
    for lc in rows:
        if lc.status != "CLOSED" or not lc.closed_at:
            continue
        closed = lc.closed_at if lc.closed_at.tzinfo else lc.closed_at.replace(tzinfo=UTC)
        if not (start <= closed <= end):
            continue
        sym = str(lc.symbol).upper()
        src = source_by_sym.get(sym) or "unknown"
        by_source_pnls[src].append(float(lc.realized_pl or 0.0))

    sources_out = {src: _pack(pnls) for src, pnls in sorted(by_source_pnls.items())}

    return {
        "lookback_days": lookback_days,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "min_trades_for_signal": min_trades_for_signal,
        "by_symbol": symbols_out,
        "by_horizon": horizons_out,
        "by_source": sources_out,
        "notes": [
            "Observational only — do not auto-tune risk or prompts from these stats.",
            "Prefer pausing or deprioritizing repeated negative-signal names with adequate sample size.",
        ],
    }
