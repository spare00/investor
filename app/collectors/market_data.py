"""Market data collectors."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

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

    async def fetch_quotes(self, symbols: list[str]) -> list[RawMarketQuote]:
        from app.market.live_prices import requires_live_market_prices

        if requires_live_market_prices():
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


class AlpacaMarketDataProvider:
    """Live Alpaca Market Data API quotes (snapshots → last trade / NBBO)."""

    name = "alpaca"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def fetch_quotes(self, symbols: list[str]) -> list[RawMarketQuote]:
        settings = self.settings
        if not settings.enable_external_data or not settings.enable_market_data_collection:
            logger.warning("alpaca_market_disabled", action="empty")
            return []
        if not settings.alpaca_api_key or not settings.alpaca_api_secret:
            logger.warning("alpaca_market_missing_keys", action="fail_closed_empty")
            return []

        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret.get_secret_value(),
        }
        syms = sorted({s.upper() for s in symbols if s})
        if not syms:
            return []

        url = f"{settings.alpaca_data_url.rstrip('/')}/v2/stocks/snapshots"
        timeout = float(settings.provider_request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.get(url, params={"symbols": ",".join(syms)}, headers=headers)
            resp.raise_for_status()
            payload = resp.json()

        now = datetime.now(UTC)
        out: list[RawMarketQuote] = []
        # Alpaca may return a top-level map of symbol → snapshot, or {"snapshots": {...}}
        snaps = payload.get("snapshots") if isinstance(payload.get("snapshots"), dict) else payload
        if not isinstance(snaps, dict):
            logger.warning("alpaca_snapshots_unexpected_shape", keys=list(payload.keys())[:8])
            return []

        for sym in syms:
            snap = snaps.get(sym) or snaps.get(sym.upper())
            if not isinstance(snap, dict):
                logger.warning("alpaca_snapshot_missing", symbol=sym)
                continue
            trade = snap.get("latestTrade") or {}
            quote = snap.get("latestQuote") or {}
            daily = snap.get("dailyBar") or snap.get("prevDailyBar") or {}
            last = (
                trade.get("p")
                or quote.get("ap")
                or quote.get("bp")
                or daily.get("c")
                or 0.0
            )
            try:
                last_f = float(last)
            except (TypeError, ValueError):
                last_f = 0.0
            if last_f <= 0:
                logger.warning("alpaca_snapshot_no_price", symbol=sym)
                continue
            bid = quote.get("bp")
            ask = quote.get("ap")
            try:
                bid_f = float(bid) if bid is not None else None
                ask_f = float(ask) if ask is not None else None
            except (TypeError, ValueError):
                bid_f, ask_f = None, None
            ts = trade.get("t") or quote.get("t")
            as_of = now
            if ts:
                try:
                    as_of = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    as_of = now
            out.append(
                RawMarketQuote(
                    symbol=sym,
                    as_of=as_of,
                    provider=self.name,
                    last=last_f,
                    open=float(daily["o"]) if daily.get("o") is not None else None,
                    high=float(daily["h"]) if daily.get("h") is not None else None,
                    low=float(daily["l"]) if daily.get("l") is not None else None,
                    volume=float(daily["v"]) if daily.get("v") is not None else None,
                    bid=bid_f,
                    ask=ask_f,
                    raw_payload={"snapshot": snap},
                )
            )
        logger.info("alpaca_quotes_fetched", requested=len(syms), returned=len(out))
        return out


def get_market_data_provider(name: str | None = None) -> MarketDataProvider:
    settings = get_settings()
    from app.market.live_prices import requires_live_market_prices

    provider_name = (name or settings.market_data_provider).lower()
    live_required = requires_live_market_prices(settings)

    if live_required:
        # Never hand callers a stub/fixture provider on the live/order path.
        if provider_name in {"stub", "fixture"}:
            logger.error(
                "stub_market_provider_forbidden",
                requested=provider_name,
                action="force_alpaca",
            )
        return AlpacaMarketDataProvider(settings)

    if provider_name == "alpaca":
        if settings.enable_external_data and settings.enable_market_data_collection:
            return AlpacaMarketDataProvider(settings)
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
