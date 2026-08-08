"""Universe outcome feedback helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.universe.outcomes import recent_outcome_stats


class _LC:
    def __init__(self, *, symbol: str, pnl: float, closed_at: datetime, horizon: str | None):
        self.symbol = symbol
        self.status = "CLOSED"
        self.closed_at = closed_at
        self.realized_pl = pnl
        self.exit_policy = {"horizon": horizon} if horizon else {}


class _WL:
    def __init__(self, symbol: str, horizon: str, source: str = "seed"):
        self.symbol = symbol
        self.horizon = horizon
        self.source = source


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Session:
    def __init__(self, watchlist, lifecycles):
        self._watchlist = watchlist
        self._lifecycles = lifecycles
        self._calls = 0

    async def execute(self, _stmt):
        self._calls += 1
        # First call: WatchlistSymbol, second: PositionLifecycle
        if self._calls == 1:
            return _Result(self._watchlist)
        return _Result(self._lifecycles)


@pytest.mark.asyncio
async def test_recent_outcome_stats_by_symbol_horizon_source() -> None:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    session = _Session(
        watchlist=[
            _WL("QQQ", "scalp", "seed"),
            _WL("MSFT", "medium", "universe_manager"),
        ],
        lifecycles=[
            _LC(symbol="QQQ", pnl=10, closed_at=now - timedelta(days=1), horizon="scalp"),
            _LC(symbol="QQQ", pnl=-5, closed_at=now - timedelta(days=2), horizon="scalp"),
            _LC(symbol="QQQ", pnl=-8, closed_at=now - timedelta(days=3), horizon="scalp"),
            _LC(symbol="MSFT", pnl=40, closed_at=now - timedelta(days=5), horizon="medium"),
            _LC(symbol="OLD", pnl=100, closed_at=now - timedelta(days=200), horizon="day"),
        ],
    )
    out = await recent_outcome_stats(session, lookback_days=90, now=now, min_trades_for_signal=3)
    by_sym = {s["symbol"]: s for s in out["by_symbol"]}
    assert by_sym["QQQ"]["trade_count"] == 3
    assert by_sym["QQQ"]["signal"] == "negative"
    assert by_sym["MSFT"]["signal"] == "insufficient"
    assert out["by_horizon"]["scalp"]["trade_count"] == 3
    assert out["by_horizon"]["medium"]["trade_count"] == 1
    assert out["by_source"]["seed"]["trade_count"] == 3
    assert "OLD" not in by_sym
