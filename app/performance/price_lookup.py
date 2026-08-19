"""Point-in-time market snapshot price lookup for decision evaluation.

Deterministic, DB-first, no look-ahead beyond the evaluation horizon end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MarketSnapshot
from app.universe.horizons import policy_for


_BOOK_LABELS = {
    "scalp": "4h",
    "day": "1session",
    "short": "10d",
    "medium": "60d",
    "unknown": "1d",
}


def evaluation_horizon_delta(book: str | None) -> timedelta:
    """Holding-policy window used as the decision evaluation horizon."""
    key = (book or "unknown").lower()
    try:
        mins = int(policy_for(key).max_holding_minutes or 0)
    except ValueError:
        mins = 0
    if mins <= 0:
        mins = 24 * 60
    return timedelta(minutes=mins)


def evaluation_horizon_label(book: str | None) -> str:
    key = (book or "unknown").lower()
    return _BOOK_LABELS.get(key, "1d")


def decision_price_max_skew(book: str | None) -> timedelta:
    """How far before decision_ts a snapshot may sit and still count as decision price."""
    key = (book or "unknown").lower()
    if key in {"scalp", "day"}:
        return timedelta(hours=6)
    if key == "short":
        return timedelta(days=2)
    if key == "medium":
        return timedelta(days=5)
    return timedelta(days=1)


def pick_price_at_or_before(
    points: list[tuple[datetime, float]],
    as_of: datetime,
    *,
    max_skew: timedelta | None = None,
) -> tuple[float | None, datetime | None]:
    """Latest price with stamp <= as_of (and optionally within max_skew)."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    eligible: list[tuple[datetime, float]] = []
    for ts, px in points:
        t = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        if t <= as_of:
            eligible.append((t, float(px)))
    if not eligible:
        return None, None
    eligible.sort(key=lambda x: x[0])
    ts, px = eligible[-1]
    if max_skew is not None and ts < as_of - max_skew:
        return None, None
    return px, ts


def pick_price_in_window(
    points: list[tuple[datetime, float]],
    start: datetime,
    end: datetime,
) -> tuple[float | None, datetime | None]:
    """Latest price with start <= stamp <= end (no look-ahead past end)."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    eligible: list[tuple[datetime, float]] = []
    for ts, px in points:
        t = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        if start <= t <= end:
            eligible.append((t, float(px)))
    if not eligible:
        return None, None
    eligible.sort(key=lambda x: x[0])
    ts, px = eligible[-1]
    return px, ts


def zone_mid(zone: dict[str, Any] | None) -> float | None:
    if not isinstance(zone, dict):
        return None
    try:
        lo = zone.get("min")
        hi = zone.get("max")
        if lo is not None and hi is not None:
            return (float(lo) + float(hi)) / 2.0
        if lo is not None:
            return float(lo)
        if hi is not None:
            return float(hi)
    except (TypeError, ValueError):
        return None
    return None


@dataclass(slots=True)
class ResolvedPrice:
    price: float | None
    as_of: datetime | None
    source: str  # payload | entry_zone | market_snapshot | missing | pending


class DecisionPriceResolver:
    """Batch-scoped snapshot resolver with per-symbol cache."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        history_start: datetime | None = None,
    ) -> None:
        self.session = session
        self.now = now or datetime.now(UTC)
        self.history_start = history_start
        self._points: dict[str, list[tuple[datetime, float]]] = {}

    async def _load_points(self, symbol: str) -> list[tuple[datetime, float]]:
        sym = symbol.upper()
        if sym in self._points:
            return self._points[sym]
        q = (
            select(MarketSnapshot.as_of, MarketSnapshot.last)
            .where(MarketSnapshot.symbol == sym)
            .order_by(MarketSnapshot.as_of.asc())
        )
        if self.history_start is not None:
            q = q.where(MarketSnapshot.as_of >= self.history_start)
        q = q.where(MarketSnapshot.as_of <= self.now)
        result = await self.session.execute(q)
        points = [(row[0], float(row[1])) for row in result.all() if row[1] is not None]
        self._points[sym] = points
        return points

    async def decision_price(
        self,
        symbol: str,
        decision_ts: datetime,
        *,
        book: str | None = None,
        explicit: float | None = None,
        entry_zone: dict[str, Any] | None = None,
    ) -> ResolvedPrice:
        if explicit is not None and float(explicit) > 0:
            return ResolvedPrice(float(explicit), decision_ts, "payload")
        mid = zone_mid(entry_zone)
        if mid is not None and mid > 0:
            # Prefer a real print when available; zone is fallback.
            points = await self._load_points(symbol)
            px, ts = pick_price_at_or_before(
                points, decision_ts, max_skew=decision_price_max_skew(book)
            )
            if px is not None:
                return ResolvedPrice(px, ts, "market_snapshot")
            return ResolvedPrice(mid, decision_ts, "entry_zone")
        points = await self._load_points(symbol)
        px, ts = pick_price_at_or_before(
            points, decision_ts, max_skew=decision_price_max_skew(book)
        )
        if px is not None:
            return ResolvedPrice(px, ts, "market_snapshot")
        return ResolvedPrice(None, None, "missing")

    async def horizon_price(
        self,
        symbol: str,
        decision_ts: datetime,
        *,
        book: str | None = None,
        explicit: float | None = None,
    ) -> ResolvedPrice:
        if explicit is not None and float(explicit) > 0:
            return ResolvedPrice(float(explicit), None, "payload")
        delta = evaluation_horizon_delta(book)
        if decision_ts.tzinfo is None:
            decision_ts = decision_ts.replace(tzinfo=UTC)
        horizon_end = decision_ts + delta
        if self.now < horizon_end:
            return ResolvedPrice(None, None, "pending")
        points = await self._load_points(symbol)
        px, ts = pick_price_in_window(points, decision_ts, horizon_end)
        if px is not None:
            return ResolvedPrice(px, ts, "market_snapshot")
        return ResolvedPrice(None, None, "missing")

    async def benchmark_return(
        self,
        benchmark: str,
        decision_ts: datetime,
        *,
        book: str | None = None,
    ) -> float | None:
        delta = evaluation_horizon_delta(book)
        if decision_ts.tzinfo is None:
            decision_ts = decision_ts.replace(tzinfo=UTC)
        horizon_end = decision_ts + delta
        if self.now < horizon_end:
            return None
        points = await self._load_points(benchmark)
        p0, _ = pick_price_at_or_before(
            points, decision_ts, max_skew=decision_price_max_skew(book)
        )
        p1, _ = pick_price_in_window(points, decision_ts, horizon_end)
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return (p1 - p0) / p0
