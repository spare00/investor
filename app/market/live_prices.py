"""Live market prices for execution — never stub/fixture prints.

Orders must be sized and limited against the present market. Hardcoded stub
quotes are allowed only for offline simulation when broker orders are off.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterable

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_SIMULATION_PROVIDERS = frozenset({"stub", "fixture"})


def requires_live_market_prices(settings: Settings | None = None) -> bool:
    """True whenever broker submission or live market collection is enabled."""
    cfg = settings or get_settings()
    return bool(
        cfg.enable_broker_orders
        or cfg.enable_automated_execution
        or cfg.enable_market_data_collection
        or cfg.enable_external_data
    )


def is_simulation_price_provider(provider: str | None) -> bool:
    return (provider or "").strip().lower() in _SIMULATION_PROVIDERS


def looks_like_stub_last(symbol: str, last: float, *, tol: float = 0.02) -> bool:
    """True if last matches the known stub table (within relative tolerance)."""
    from app.collectors.market_data import _STUB_LAST

    stub = _STUB_LAST.get(symbol.upper())
    if stub is None or last <= 0:
        return False
    return abs(float(last) - float(stub)) / float(stub) <= tol


async def fetch_live_last_prices(
    symbols: Iterable[str],
    *,
    settings: Settings | None = None,
) -> dict[str, float]:
    """Fetch current last prints from Alpaca snapshots only."""
    from app.collectors.market_data import AlpacaMarketDataProvider

    cfg = settings or get_settings()
    syms = sorted({str(s).upper() for s in symbols if s})
    if not syms:
        return {}
    quotes = await AlpacaMarketDataProvider(cfg).fetch_quotes(syms)
    out: dict[str, float] = {}
    for q in quotes:
        if q.provider and is_simulation_price_provider(q.provider):
            logger.error("live_price_fetch_returned_simulation_provider", symbol=q.symbol)
            continue
        if q.last is None or float(q.last) <= 0:
            continue
        if looks_like_stub_last(q.symbol, float(q.last)):
            # Guard against accidental stub wiring behind an "alpaca" label.
            logger.error(
                "live_price_matches_stub_table_rejected",
                symbol=q.symbol,
                last=q.last,
            )
            continue
        out[q.symbol.upper()] = float(q.last)
    logger.info(
        "live_prices_fetched",
        requested=len(syms),
        returned=len(out),
        as_of=datetime.now(UTC).isoformat(),
    )
    return out


async def resolve_execution_prices(
    symbols: Iterable[str],
    *,
    candidate_prices: dict[str, float] | None = None,
    settings: Settings | None = None,
) -> tuple[dict[str, float], list[str]]:
    """Return prices safe for order sizing/submit.

    When live prices are required, candidates are ignored — never fall back to
    stub/collection leftovers for execution.
    """
    cfg = settings or get_settings()
    notes: list[str] = []
    syms = sorted({str(s).upper() for s in symbols if s})
    if not requires_live_market_prices(cfg):
        cleaned = {
            k.upper(): float(v)
            for k, v in (candidate_prices or {}).items()
            if v and float(v) > 0
        }
        notes.append("simulation_prices_allowed")
        return cleaned, notes

    live = await fetch_live_last_prices(syms, settings=cfg)
    if not live:
        notes.append("live_prices_unavailable")
        return {}, notes

    missing = [s for s in syms if s not in live]
    if missing:
        notes.append(f"live_price_missing:{','.join(missing[:12])}")
    return live, notes


def assert_provider_allowed_for_orders(
    provider: str | None, *, settings: Settings | None = None
) -> None:
    cfg = settings or get_settings()
    if requires_live_market_prices(cfg) and is_simulation_price_provider(provider):
        raise RuntimeError(f"simulation_price_provider_forbidden:{provider or 'unknown'}")
