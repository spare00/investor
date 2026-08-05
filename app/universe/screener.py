"""Liquidity screener for the bounded candidate pool (not full-market).

Filters curated/ranked candidates by ADV, spread, and price using DB snapshots
and optional live quote fill. Fail soft on missing quotes when live fetch is off
(keep name, annotate) so offline/dev still has a pool; fail closed on known bad
liquidity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models import MarketSnapshot

logger = get_logger(__name__)


@dataclass(slots=True)
class ScreenHit:
    symbol: str
    passed: bool
    reasons: tuple[str, ...] = ()
    last: float | None = None
    avg_volume_20d: float | None = None
    spread_bps: float | None = None


@dataclass(slots=True)
class ScreenResult:
    passed: list[str] = field(default_factory=list)
    rejected: list[ScreenHit] = field(default_factory=list)
    skipped_no_data: list[str] = field(default_factory=list)
    source: str = "disabled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": list(self.passed),
            "rejected": [
                {
                    "symbol": h.symbol,
                    "reasons": list(h.reasons),
                    "last": h.last,
                    "avg_volume_20d": h.avg_volume_20d,
                    "spread_bps": h.spread_bps,
                }
                for h in self.rejected
            ],
            "skipped_no_data": list(self.skipped_no_data),
            "source": self.source,
            "passed_count": len(self.passed),
            "rejected_count": len(self.rejected),
        }


def evaluate_liquidity(
    *,
    symbol: str,
    last: float | None,
    avg_volume_20d: float | None,
    spread_bps: float | None,
    settings: Settings,
) -> ScreenHit:
    reasons: list[str] = []
    min_vol = float(settings.universe_screener_min_avg_volume)
    max_spread = float(settings.universe_screener_max_spread_bps)
    min_price = float(settings.universe_screener_min_price)

    if last is not None and last < min_price:
        reasons.append("penny_or_low_price")
    if avg_volume_20d is not None and avg_volume_20d < min_vol:
        reasons.append("insufficient_volume")
    if spread_bps is not None and spread_bps > max_spread:
        reasons.append("excessive_spread")
    if last is not None and last <= 0:
        reasons.append("invalid_price")

    return ScreenHit(
        symbol=symbol.upper(),
        passed=not reasons,
        reasons=tuple(reasons),
        last=last,
        avg_volume_20d=avg_volume_20d,
        spread_bps=spread_bps,
    )


async def _latest_snapshots(
    session: AsyncSession, symbols: list[str]
) -> dict[str, MarketSnapshot]:
    out: dict[str, MarketSnapshot] = {}
    for sym in symbols:
        row = (
            await session.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.symbol == sym)
                .order_by(desc(MarketSnapshot.as_of))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            out[sym] = row
    return out


async def _fetch_missing_quotes(symbols: list[str], settings: Settings) -> dict[str, dict[str, float | None]]:
    if not symbols:
        return {}
    from app.collectors.market_data import get_market_data_provider
    from app.services.normalize import normalize_market_quote, spread_bps

    provider = get_market_data_provider()
    raw = await provider.fetch_quotes(symbols)
    out: dict[str, dict[str, float | None]] = {}
    for q in raw:
        norm = normalize_market_quote(q)
        spread = norm.spread_bps
        if spread is None and norm.bid and norm.ask and norm.last:
            spread = spread_bps(norm.bid, norm.ask, mid=norm.last)
        out[norm.symbol.upper()] = {
            "last": norm.last,
            "avg_volume_20d": norm.avg_volume_20d,
            "spread_bps": spread,
        }
    return out


async def screen_candidates(
    session: AsyncSession,
    settings: Settings,
    symbols: list[str],
) -> ScreenResult:
    """Filter candidate symbols by liquidity bars."""
    ordered = [s.upper().strip() for s in symbols if s and str(s).strip()]
    if not settings.universe_screener_enabled:
        return ScreenResult(passed=ordered, source="disabled")

    snaps = await _latest_snapshots(session, ordered)
    missing = [s for s in ordered if s not in snaps]
    fetched: dict[str, dict[str, float | None]] = {}
    source = "db"
    if missing and settings.universe_screener_fetch_live:
        try:
            fetched = await _fetch_missing_quotes(missing, settings)
            source = "db+provider" if snaps else "provider"
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe_screener_fetch_failed", error=str(exc))
            source = "db"
    elif missing and not snaps:
        # No DB data at all — use provider once so first-run / stub environments still screen.
        try:
            fetched = await _fetch_missing_quotes(missing, settings)
            source = "provider"
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe_screener_fetch_failed", error=str(exc))
            source = "empty"

    passed: list[str] = []
    rejected: list[ScreenHit] = []
    skipped: list[str] = []

    for sym in ordered:
        last = avg = spread = None
        if sym in snaps:
            row = snaps[sym]
            last, avg, spread = row.last, row.avg_volume_20d, row.spread_bps
        elif sym in fetched:
            meta = fetched[sym]
            last = meta.get("last")
            avg = meta.get("avg_volume_20d")
            spread = meta.get("spread_bps")
        else:
            skipped.append(sym)
            # No data: keep in pool but do not treat as hard reject (offline-friendly).
            passed.append(sym)
            continue

        hit = evaluate_liquidity(
            symbol=sym,
            last=last,
            avg_volume_20d=avg,
            spread_bps=spread,
            settings=settings,
        )
        if hit.passed:
            passed.append(sym)
        else:
            rejected.append(hit)

    logger.info(
        "universe_screener_done",
        passed=len(passed),
        rejected=len(rejected),
        skipped=len(skipped),
        source=source,
    )
    return ScreenResult(
        passed=passed,
        rejected=rejected,
        skipped_no_data=skipped,
        source=source,
    )
