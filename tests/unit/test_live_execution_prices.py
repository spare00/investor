"""Tests: live prices required for order path; stubs never used for execution."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.collectors.market_data import (
    AlpacaMarketDataProvider,
    StubMarketDataProvider,
    get_market_data_provider,
)
from app.core.config import Settings
from app.market.live_prices import (
    looks_like_stub_last,
    requires_live_market_prices,
    resolve_execution_prices,
)


def test_requires_live_when_broker_orders_on() -> None:
    assert (
        requires_live_market_prices(
            Settings(enable_broker_orders=True, broker_provider="alpaca")
        )
        is True
    )
    assert (
        requires_live_market_prices(
            Settings(enable_broker_orders=True, broker_provider="mock")
        )
        is False
    )
    assert (
        requires_live_market_prices(
            Settings(
                enable_broker_orders=False,
                enable_automated_execution=False,
                enable_market_data_collection=False,
                enable_external_data=False,
            )
        )
        is False
    )


def test_looks_like_stub_last_detects_hardcoded_aapl() -> None:
    assert looks_like_stub_last("AAPL", 220.0) is True
    assert looks_like_stub_last("AAPL", 310.0) is False


@pytest.mark.asyncio
async def test_resolve_execution_prices_ignores_stub_candidates_when_live_required() -> None:
    settings = Settings(
        enable_broker_orders=True,
        enable_external_data=True,
        enable_market_data_collection=True,
        alpaca_api_key="k",
        alpaca_api_secret="s",
    )
    with patch(
        "app.market.live_prices.fetch_live_last_prices",
        new=AsyncMock(return_value={"AAPL": 310.5}),
    ):
        prices, notes = await resolve_execution_prices(
            ["AAPL"],
            candidate_prices={"AAPL": 220.0},
            settings=settings,
        )
    assert prices == {"AAPL": 310.5}
    assert "live_prices_unavailable" not in notes


@pytest.mark.asyncio
async def test_resolve_execution_prices_fail_closed_when_live_empty() -> None:
    settings = Settings(enable_broker_orders=True, enable_external_data=True)
    with patch(
        "app.market.live_prices.fetch_live_last_prices",
        new=AsyncMock(return_value={}),
    ):
        prices, notes = await resolve_execution_prices(
            ["AAPL"],
            candidate_prices={"AAPL": 220.0},
            settings=settings,
        )
    assert prices == {}
    assert "live_prices_unavailable" in notes


@pytest.mark.asyncio
async def test_stub_provider_returns_empty_when_live_required() -> None:
    with patch(
        "app.market.live_prices.requires_live_market_prices",
        return_value=True,
    ):
        out = await StubMarketDataProvider().fetch_quotes(["AAPL"])
    assert out == []


def test_get_market_data_provider_forces_alpaca_when_live_required() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_broker_orders=True,
            enable_external_data=True,
            enable_market_data_collection=True,
            market_data_provider="stub",
        ),
    ):
        provider = get_market_data_provider("stub")
    assert isinstance(provider, AlpacaMarketDataProvider)
