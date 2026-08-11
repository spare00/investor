"""Market data collectors."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.collectors.base import MarketDataProvider, RawMarketQuote
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Static stub quotes roughly centered for allowlist symbols (paper/dev only).
_STUB_LAST: dict[str, float] = {
    "SPY": 560.0,
    "QQQ": 480.0,
    "IWM": 220.0,
    "DIA": 410.0,
    "NVDA": 120.0,
    "MSFT": 430.0,
    "AMZN": 190.0,
    "GOOGL": 175.0,
    "META": 520.0,
    "AVGO": 160.0,
    "AMD": 160.0,
    "AAPL": 220.0,
    "TSLA": 250.0,
    "IONQ": 12.0,
}


class StubMarketDataProvider:
    name = "stub"

    def __init__(self, quotes: dict[str, float] | None = None) -> None:
        self._quotes = quotes or _STUB_LAST

    async def fetch_quotes(
        self, symbols: list[str], *, allow_stub: bool = False, con_ids: dict[str, int] | None = None
    ) -> list[RawMarketQuote]:
        from app.market.live_prices import requires_live_market_prices

        _ = con_ids
        if requires_live_market_prices() and not allow_stub:
            logger.error(
                "stub_quotes_blocked",
                reason="live_market_prices_required",
                symbols=len(symbols),
            )
            return []
        now = datetime.now(UTC)
        out: list[RawMarketQuote] = []
        for symbol in symbols:
            sym = symbol.upper()
            last = self._quotes.get(sym)
            if last is None:
                # Synthetic quote so existing off-map holdings remain manageable in stub mode.
                last = 10.0 + (sum(ord(c) for c in sym) % 90)
                logger.info("stub_quote_synthetic", symbol=sym, last=last)
            bid = round(last * 0.9999, 4)
            ask = round(last * 1.0001, 4)
            out.append(
                RawMarketQuote(
                    symbol=sym,
                    as_of=now,
                    provider=self.name,
                    last=float(last),
                    bid=bid,
                    ask=ask,
                    volume=25_000_000.0,
                    raw_payload={"stub": True, "last": last},
                )
            )
        return out


# Process-local IBKR market-data session (dedicated clientId; shared across fetches).
_IBKR_MD_IB: Any | None = None
_IBKR_MD_LOCK = asyncio.Lock()


class IbkrMarketDataProvider:
    """Live quotes via IB Gateway (TWS API). Uses a dedicated clientId.

    Connection is reused across ``fetch_quotes`` calls — reconnecting every poll
    is expensive and can exhaust Gateway client slots under the scheduler.
    """

    name = "ibkr"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def _ensure_connected(self) -> Any | None:
        """Return a connected readonly IB client, or None on failure."""
        global _IBKR_MD_IB
        settings = self.settings
        async with _IBKR_MD_LOCK:
            if _IBKR_MD_IB is not None and _IBKR_MD_IB.isConnected():
                return _IBKR_MD_IB
            try:
                from ib_async import IB
            except ImportError:
                logger.error("ib_async_not_installed")
                return None

            if _IBKR_MD_IB is not None:
                try:
                    if _IBKR_MD_IB.isConnected():
                        _IBKR_MD_IB.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                _IBKR_MD_IB = None

            host = settings.ibkr_host
            port = int(settings.ibkr_port)
            client_id = int(settings.ibkr_md_client_id or (int(settings.ibkr_client_id) + 10))
            timeout = max(5, int(settings.provider_request_timeout_seconds))
            ib = IB()
            try:
                await ib.connectAsync(
                    host, port, clientId=client_id, readonly=True, timeout=timeout
                )
                # Paper accounts often lack real-time API entitlements; use delayed when needed.
                try:
                    ib.reqMarketDataType(3)  # 3 = delayed
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "ibkr_md_connect_failed", host=host, port=port, error=str(exc)[:160]
                )
                return None
            if not ib.isConnected():
                logger.error("ibkr_md_not_connected", host=host, port=port)
                return None
            _IBKR_MD_IB = ib
            logger.info(
                "ibkr_md_connected",
                host=host,
                port=port,
                client_id=client_id,
            )
            return ib

    @classmethod
    async def disconnect(cls) -> None:
        """Drop the shared MD session (tests / broker disconnect)."""
        global _IBKR_MD_IB
        async with _IBKR_MD_LOCK:
            if _IBKR_MD_IB is not None:
                try:
                    if _IBKR_MD_IB.isConnected():
                        _IBKR_MD_IB.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                _IBKR_MD_IB = None

    async def fetch_quotes(
        self,
        symbols: list[str],
        *,
        con_ids: dict[str, int] | None = None,
    ) -> list[RawMarketQuote]:
        settings = self.settings
        if not settings.enable_external_data or not settings.enable_market_data_collection:
            logger.warning("ibkr_market_disabled", action="empty")
            return []
        if (settings.broker_environment or "").lower() != "paper" and settings.enable_live_trading:
            logger.error("ibkr_market_live_blocked")
            return []

        syms = sorted({s.upper() for s in symbols if s})
        if not syms:
            return []

        try:
            from ib_async import Stock
        except ImportError:
            logger.error("ib_async_not_installed")
            return []

        ib = await self._ensure_connected()
        if ib is None:
            return []

        now = datetime.now(UTC)
        out: list[RawMarketQuote] = []
        # reqTickersAsync can hang indefinitely on a wedged Gateway session; bound it.
        timeout = max(5, int(settings.provider_request_timeout_seconds))
        try:
            out = await asyncio.wait_for(
                self._fetch_quotes_inner(ib, Stock, syms, con_ids=con_ids, now=now),
                timeout=float(timeout),
            )
        except TimeoutError:
            logger.error(
                "ibkr_md_fetch_timeout",
                requested=len(syms),
                timeout_s=timeout,
            )
            await self.disconnect()
            return []
        except Exception:  # noqa: BLE001
            logger.exception("ibkr_md_fetch_failed")
            # Drop sticky broken sessions so the next poll reconnects cleanly.
            await self.disconnect()
            return []

        logger.info("ibkr_quotes_fetched", requested=len(syms), returned=len(out))
        return out

    async def _fetch_quotes_inner(
        self,
        ib: Any,
        stock_cls: Any,
        syms: list[str],
        *,
        con_ids: dict[str, int] | None,
        now: datetime,
    ) -> list[RawMarketQuote]:
        out: list[RawMarketQuote] = []
        contracts = []
        for sym in syms:
            cid = None
            if con_ids:
                raw = con_ids.get(sym) or con_ids.get(sym.upper())
                cid = int(raw) if raw else None
            contract = await self._qualify(ib, stock_cls, sym, con_id=cid)
            if contract is not None:
                contracts.append(contract)
        if not contracts:
            return []
        tickers = await ib.reqTickersAsync(*contracts)
        # Brief settle for delayed ticks.
        await asyncio.sleep(0.8)
        tickers = await ib.reqTickersAsync(*contracts)
        by_sym = {str(t.contract.symbol).upper(): t for t in tickers if t.contract}
        for sym in syms:
            t = by_sym.get(sym)
            if t is None:
                logger.warning("ibkr_ticker_missing", symbol=sym)
                continue
            last = self._last_price(t)
            if last is None or last <= 0:
                logger.warning("ibkr_ticker_no_price", symbol=sym)
                continue
            bid = float(t.bid) if t.bid and t.bid > 0 else None
            ask = float(t.ask) if t.ask and t.ask > 0 else None
            out.append(
                RawMarketQuote(
                    symbol=sym,
                    as_of=now,
                    provider=self.name,
                    last=float(last),
                    bid=bid,
                    ask=ask,
                    volume=float(t.volume) if t.volume and t.volume > 0 else None,
                    raw_payload={
                        "con_id": getattr(t.contract, "conId", None),
                        "exchange": getattr(t.contract, "primaryExchange", None)
                        or getattr(t.contract, "exchange", None),
                        "currency": getattr(t.contract, "currency", None),
                        "last": last,
                        "close": getattr(t, "close", None),
                        "market_price": getattr(t, "marketPrice", lambda: None)(),
                    },
                )
            )
        return out

    async def _qualify(
        self,
        ib: Any,
        stock_cls: Any,
        symbol: str,
        *,
        con_id: int | None = None,
    ) -> Any | None:
        from app.brokers.ibkr_contracts import resolve_stock_contract
        from app.market.venues import venue_for_symbol

        try:
            venue = venue_for_symbol(symbol, self.settings).value
            return await resolve_stock_contract(
                ib,
                symbol=symbol,
                con_id=con_id,
                venue=venue,
                settings=self.settings,
                stock_cls=stock_cls,
            )
        except LookupError:
            logger.warning("ibkr_md_qualify_failed", symbol=symbol, con_id=con_id)
            return None

    @staticmethod
    def _last_price(ticker: Any) -> float | None:
        for attr in ("last", "close", "midpoint"):
            val = getattr(ticker, attr, None)
            if callable(val):
                try:
                    val = val()
                except Exception:  # noqa: BLE001
                    val = None
            try:
                f = float(val) if val is not None else 0.0
            except (TypeError, ValueError):
                f = 0.0
            if f > 0:
                return f
        mp = getattr(ticker, "marketPrice", None)
        if callable(mp):
            try:
                f = float(mp())
                if f > 0:
                    return f
            except Exception:  # noqa: BLE001
                return None
        return None


def get_market_data_provider(name: str | None = None) -> MarketDataProvider:
    settings = get_settings()
    from app.market.live_prices import requires_live_market_prices

    provider_name = (name or settings.market_data_provider or "ibkr").lower()
    if (settings.broker_provider or "").lower() == "ibkr" and provider_name in {"auto", ""}:
        provider_name = "ibkr"
    live_required = requires_live_market_prices(settings)

    if live_required:
        if provider_name in {"stub", "fixture"}:
            logger.error(
                "stub_market_provider_forbidden",
                requested=provider_name,
                action="force_ibkr_provider",
            )
            provider_name = "ibkr"
        if provider_name != "ibkr":
            logger.error(
                "unsupported_live_market_provider",
                requested=provider_name,
                action="force_ibkr_provider",
            )
            provider_name = "ibkr"
        return IbkrMarketDataProvider(settings)

    if provider_name == "ibkr":
        if settings.enable_external_data and settings.enable_market_data_collection:
            return IbkrMarketDataProvider(settings)
        logger.info(
            "market_provider_fallback_stub",
            requested=provider_name,
            reason="external_market_data_disabled",
        )
        return StubMarketDataProvider()
    if provider_name in {"stub", "fixture"}:
        return StubMarketDataProvider()
    logger.info("market_provider_unknown_using_stub", requested=provider_name)
    return StubMarketDataProvider()
