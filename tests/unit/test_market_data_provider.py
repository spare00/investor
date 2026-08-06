"""Tests for market data provider selection and Alpaca snapshot parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.collectors.market_data import (
    AlpacaMarketDataProvider,
    StubMarketDataProvider,
    get_market_data_provider,
)
from app.core.config import Settings


@pytest.mark.asyncio
async def test_alpaca_provider_parses_snapshots() -> None:
    settings = Settings(
        enable_external_data=True,
        enable_market_data_collection=True,
        alpaca_api_key="key",
        alpaca_api_secret="secret",
    )
    provider = AlpacaMarketDataProvider(settings)
    payload = {
        "AAPL": {
            "latestTrade": {"p": 310.31, "t": "2026-08-06T15:50:00Z"},
            "latestQuote": {"bp": 310.2, "ap": 310.4, "t": "2026-08-06T15:50:00Z"},
            "dailyBar": {"o": 308.0, "h": 312.0, "l": 307.0, "c": 310.0, "v": 1e7},
        }
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.collectors.market_data.httpx.AsyncClient", return_value=client):
        quotes = await provider.fetch_quotes(["AAPL"])

    assert len(quotes) == 1
    assert quotes[0].symbol == "AAPL"
    assert quotes[0].last == pytest.approx(310.31)
    assert quotes[0].provider == "alpaca"
    assert quotes[0].bid == pytest.approx(310.2)


def test_get_market_data_provider_uses_alpaca_when_enabled() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_external_data=True,
            enable_market_data_collection=True,
            market_data_provider="alpaca",
        ),
    ):
        provider = get_market_data_provider()
    assert isinstance(provider, AlpacaMarketDataProvider)


def test_get_market_data_provider_falls_back_to_stub_when_disabled() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_external_data=False,
            enable_market_data_collection=False,
            enable_broker_orders=False,
            enable_automated_execution=False,
            market_data_provider="alpaca",
        ),
    ), patch(
        "app.market.live_prices.requires_live_market_prices",
        return_value=False,
    ):
        provider = get_market_data_provider()
    assert isinstance(provider, StubMarketDataProvider)
