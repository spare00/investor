"""Market data collectors."""

from __future__ import annotations

from datetime import UTC, datetime

from app.collectors.base import MarketDataProvider, RawMarketQuote
from app.core.config import get_settings
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
        now = datetime.now(UTC)
        out: list[RawMarketQuote] = []
        for symbol in symbols:
            sym = symbol.upper()
            last = self._quotes.get(sym)
            if last is None:
                logger.warning("stub_quote_missing", symbol=sym)
                continue
            bid = round(last * 0.9999, 4)
            ask = round(last * 1.0001, 4)
            out.append(
                RawMarketQuote(
                    symbol=sym,
                    as_of=now,
                    provider=self.name,
                    last=last,
                    open=last * 0.998,
                    high=last * 1.01,
                    low=last * 0.99,
                    volume=25_000_000,
                    avg_volume_20d=30_000_000,
                    atr_14=round(last * 0.015, 4),
                    rsi_14=55.0,
                    sma_20=last * 0.99,
                    sma_50=last * 0.97,
                    sma_200=last * 0.92,
                    bid=bid,
                    ask=ask,
                    premarket_change_pct=0.35,
                    gap_pct=0.2,
                    vix=16.5 if sym == "SPY" else None,
                    raw_payload={"stub": True},
                )
            )
        return out


class AlpacaMarketDataProvider:
    """Scaffold — real Alpaca data client in Phase 6; fail-closed without keys."""

    name = "alpaca"

    async def fetch_quotes(self, symbols: list[str]) -> list[RawMarketQuote]:
        settings = get_settings()
        if not settings.alpaca_api_key or not settings.alpaca_api_secret:
            logger.warning("alpaca_market_missing_keys", action="fail_closed_empty")
            return []
        logger.warning("alpaca_market_not_wired", symbols=symbols)
        return []


def get_market_data_provider(name: str | None = None) -> MarketDataProvider:
    settings = get_settings()
    provider_name = (name or settings.market_data_provider).lower()
    if provider_name == "alpaca":
        # Until wired, fall back to stub in non-production so local/dev works.
        if settings.app_env.value == "production":
            return AlpacaMarketDataProvider()
        logger.info("market_provider_fallback_stub", requested=provider_name)
        return StubMarketDataProvider()
    return StubMarketDataProvider()
