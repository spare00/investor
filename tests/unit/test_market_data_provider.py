"""Tests for market data provider selection (stub / IBKR)."""

from __future__ import annotations

from unittest.mock import patch

from app.collectors.market_data import (
    IbkrMarketDataProvider,
    StubMarketDataProvider,
    get_market_data_provider,
)
from app.core.config import Settings


def test_get_market_data_provider_uses_ibkr_when_enabled() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_external_data=True,
            enable_market_data_collection=True,
            market_data_provider="ibkr",
            broker_provider="ibkr",
            enable_broker_orders=False,
            enable_automated_execution=False,
        ),
    ), patch(
        "app.market.live_prices.requires_live_market_prices",
        return_value=False,
    ):
        provider = get_market_data_provider()
    assert isinstance(provider, IbkrMarketDataProvider)


def test_get_market_data_provider_falls_back_to_stub_when_disabled() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_external_data=False,
            enable_market_data_collection=False,
            enable_broker_orders=False,
            enable_automated_execution=False,
            market_data_provider="ibkr",
        ),
    ), patch(
        "app.market.live_prices.requires_live_market_prices",
        return_value=False,
    ):
        provider = get_market_data_provider()
    assert isinstance(provider, StubMarketDataProvider)


def test_get_market_data_provider_forces_ibkr_when_live_required() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_external_data=True,
            enable_market_data_collection=True,
            market_data_provider="stub",
            broker_provider="ibkr",
            enable_broker_orders=True,
            enable_automated_execution=True,
        ),
    ), patch(
        "app.market.live_prices.requires_live_market_prices",
        return_value=True,
    ):
        provider = get_market_data_provider()
    assert isinstance(provider, IbkrMarketDataProvider)
