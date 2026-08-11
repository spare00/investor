"""IBKR contract resolution helpers — prefer conId, cache qualifies.

IB best practice: identify contracts by ``conId`` (+ exchange) rather than
re-resolving ``symbol``/currency on every order or market-data poll.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.market.venues import ib_qualify_candidates

logger = get_logger(__name__)

# Process-local caches shared by broker + market-data clients.
_BY_CON_ID: dict[int, Any] = {}
_BY_SYMBOL_KEY: dict[tuple[str, str | None, str | None], Any] = {}


def clear_contract_cache() -> None:
    _BY_CON_ID.clear()
    _BY_SYMBOL_KEY.clear()


def _qualify_timeout(settings: Settings) -> float:
    return float(max(5, int(settings.broker_request_timeout_seconds)))


async def _qualify_async(ib: Any, contract: Any, *, settings: Settings, label: str) -> Any:
    timeout = _qualify_timeout(settings)
    try:
        return await asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout=timeout)
    except TimeoutError:
        logger.warning("ibkr_qualify_timeout", label=label, timeout_s=timeout)
        return None


def cache_contract(contract: Any) -> Any:
    """Remember a qualified contract under conId and symbol keys."""
    if contract is None:
        return contract
    con_id = int(getattr(contract, "conId", 0) or 0)
    if con_id:
        _BY_CON_ID[con_id] = contract
    symbol = str(getattr(contract, "symbol", "") or "").upper()
    if symbol:
        ccy = str(getattr(contract, "currency", "") or "") or None
        venue_hint = None
        _BY_SYMBOL_KEY[(symbol, venue_hint, ccy)] = contract
        # Also index without currency for fast conId-less lookups after first hit.
        _BY_SYMBOL_KEY.setdefault((symbol, venue_hint, None), contract)
    return contract


def contract_from_con_id(con_id: int) -> Any | None:
    cid = int(con_id or 0)
    if not cid:
        return None
    hit = _BY_CON_ID.get(cid)
    if hit is not None:
        return hit
    try:
        from ib_async import Contract
    except ImportError:  # pragma: no cover
        return None
    return Contract(conId=cid)


async def resolve_stock_contract(
    ib: Any,
    *,
    symbol: str | None = None,
    con_id: int | None = None,
    venue: str | None = None,
    currency: str | None = None,
    settings: Settings | None = None,
    stock_cls: Any | None = None,
) -> Any:
    """Return a qualified IBKR stock contract, preferring conId + cache.

    Raises ``LookupError`` when the contract cannot be resolved.
    """
    cfg = settings or get_settings()
    cid = int(con_id or 0)
    if cid:
        cached = _BY_CON_ID.get(cid)
        if cached is not None and int(getattr(cached, "conId", 0) or 0) == cid:
            return cached
        bare = contract_from_con_id(cid)
        if bare is not None:
            try:
                qualified = await _qualify_async(
                    ib, bare, settings=cfg, label=f"conId:{cid}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ibkr_conid_qualify_failed", con_id=cid, error=str(exc)[:160])
                qualified = None
            hit = next((c for c in (qualified or []) if getattr(c, "conId", 0)), None)
            if hit is not None:
                return cache_contract(hit)
            # Gateway often accepts placeOrder with conId alone after a prior qualify.
            return cache_contract(bare)

    sym = (symbol or "").upper().strip()
    if not sym:
        raise LookupError("ibkr_contract_symbol_or_conid_required")

    key = (sym, (venue or None), (currency.upper() if currency else None))
    cached = _BY_SYMBOL_KEY.get(key) or _BY_SYMBOL_KEY.get((sym, venue or None, None))
    if cached is not None:
        return cached

    if stock_cls is None:
        from ib_async import Stock

        stock_cls = Stock

    candidates = ib_qualify_candidates(cfg, venue=venue)
    if currency:
        ccy = currency.upper()
        preferred = [(ex, c) for ex, c in candidates if c == ccy]
        rest = [(ex, c) for ex, c in candidates if c != ccy]
        candidates = preferred + rest

    last_exc: Exception | None = None
    for exchange, ccy in candidates:
        contract = stock_cls(sym, exchange, ccy)
        try:
            qualified = await _qualify_async(
                ib, contract, settings=cfg, label=f"{sym}:{exchange}:{ccy}"
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        hit = next((c for c in (qualified or []) if getattr(c, "conId", 0)), None)
        if hit is not None:
            cache_contract(hit)
            _BY_SYMBOL_KEY[key] = hit
            _BY_SYMBOL_KEY[(sym, venue or None, None)] = hit
            return hit
    if last_exc is not None:
        raise LookupError(f"ibkr_qualify_failed:{sym}:{last_exc}") from last_exc
    raise LookupError(f"ibkr_contract_not_found:{sym}")
