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
    """True whenever a real broker/order path or live market collection is enabled.

    Mock/simulated brokers are offline unit/dev paths — they do not require IBKR
    prints (the mock book is the present market for those runs).
    """
    cfg = settings or get_settings()
    if (cfg.broker_provider or "").lower() in {"mock", "simulated", "sim"}:
        return False
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
    con_ids: dict[str, int] | None = None,
) -> dict[str, float]:
    """Fetch current last prints from the configured live market-data provider."""
    from app.collectors.market_data import get_market_data_provider

    cfg = settings or get_settings()
    syms = sorted({str(s).upper() for s in symbols if s})
    if not syms:
        return {}
    provider = get_market_data_provider()
    quotes = await provider.fetch_quotes(syms, con_ids=con_ids)
    out: dict[str, float] = {}
    for q in quotes:
        if q.provider and is_simulation_price_provider(q.provider):
            logger.error("live_price_fetch_returned_simulation_provider", symbol=q.symbol)
            continue
        if q.last is None or float(q.last) <= 0:
            continue
        if looks_like_stub_last(q.symbol, float(q.last)):
            # Guard against accidental stub wiring behind a live provider label.
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
        provider=cfg.market_data_provider,
        as_of=datetime.now(UTC).isoformat(),
    )
    return out


def _usable_live_candidates(
    candidate_prices: dict[str, float] | None,
    symbols: list[str],
) -> dict[str, float]:
    """Keep positive non-stub candidate prints for symbols we still need."""
    needed = set(symbols)
    out: dict[str, float] = {}
    for key, raw in (candidate_prices or {}).items():
        sym = str(key).upper()
        if sym not in needed or raw is None:
            continue
        try:
            last = float(raw)
        except (TypeError, ValueError):
            continue
        if last <= 0 or looks_like_stub_last(sym, last):
            continue
        out[sym] = last
    return out


async def resolve_execution_prices(
    symbols: Iterable[str],
    *,
    candidate_prices: dict[str, float] | None = None,
    settings: Settings | None = None,
) -> tuple[dict[str, float], list[str]]:
    """Return prices safe for order sizing/submit.

    When live prices are required, stub candidates are rejected. Fresh non-stub
    IBKR candidates may be reused to avoid a second market-data round-trip;
    any remaining symbols are fetched live. Never fall back to stub leftovers.
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

    reused = _usable_live_candidates(candidate_prices, syms)
    missing = [s for s in syms if s not in reused]
    if not missing:
        notes.append("reused_live_candidate_prices")
        return reused, notes

    if reused:
        notes.append(f"reused_live_candidate_partial:{len(reused)}")

    live = await fetch_live_last_prices(missing, settings=cfg)
    merged = {**reused, **live}
    if not merged:
        notes.append("live_prices_unavailable")
        return {}, notes

    still_missing = [s for s in syms if s not in merged]
    if still_missing:
        notes.append(f"live_price_missing:{','.join(still_missing[:12])}")
    return merged, notes


def assess_collection_price_integrity(
    *,
    providers: list[str],
    market_count: int,
    settings: Settings | None = None,
) -> tuple[bool, bool, list[str], list[str]]:
    """Return (live_required, feed_live, providers, notes) for the Risk Officer."""
    cfg = settings or get_settings()
    live_required = requires_live_market_prices(cfg)
    cleaned = sorted({(p or "").strip().lower() for p in providers if p})
    notes: list[str] = []
    if not live_required:
        return False, True, cleaned, ["simulation_price_path"]
    if market_count <= 0:
        notes.append("no_market_quotes")
        return True, False, cleaned, notes
    if any(is_simulation_price_provider(p) for p in cleaned):
        notes.append("simulation_provider_present")
        return True, False, cleaned, notes
    if not cleaned:
        notes.append("missing_provider_labels")
        return True, False, cleaned, notes
    if not any(not is_simulation_price_provider(p) for p in cleaned):
        notes.append("no_live_provider")
        return True, False, cleaned, notes
    return True, True, cleaned, notes


def assert_provider_allowed_for_orders(
    provider: str | None, *, settings: Settings | None = None
) -> None:
    cfg = settings or get_settings()
    if requires_live_market_prices(cfg) and is_simulation_price_provider(provider):
        raise RuntimeError(f"simulation_price_provider_forbidden:{provider or 'unknown'}")
