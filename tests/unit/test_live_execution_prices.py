"""Tests: live prices required for order path; stubs never used for execution."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.collectors.market_data import (
    IbkrMarketDataProvider,
    StubMarketDataProvider,
    get_market_data_provider,
)
from app.core.config import Settings
from app.market.live_prices import (
    fetch_live_last_prices,
    looks_like_stub_last,
    requires_live_market_prices,
    resolve_execution_prices,
)


def test_requires_live_when_broker_orders_on() -> None:
    assert (
        requires_live_market_prices(
            Settings(enable_broker_orders=True, broker_provider="ibkr")
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
async def test_resolve_execution_prices_reuses_non_stub_candidates() -> None:
    settings = Settings(
        enable_broker_orders=True,
        enable_external_data=True,
        enable_market_data_collection=True,
        broker_provider="ibkr",
    )
    with patch(
        "app.market.live_prices.fetch_live_last_prices",
        new=AsyncMock(return_value={"MSFT": 440.0}),
    ) as fetch:
        prices, notes = await resolve_execution_prices(
            ["AAPL", "MSFT"],
            candidate_prices={"AAPL": 310.5},
            settings=settings,
        )
    assert prices == {"AAPL": 310.5, "MSFT": 440.0}
    assert any(n.startswith("reused_live_candidate_partial") for n in notes)
    fetch.assert_awaited_once()
    assert fetch.await_args.args[0] == ["MSFT"]


@pytest.mark.asyncio
async def test_resolve_execution_prices_skips_fetch_when_candidates_cover() -> None:
    settings = Settings(
        enable_broker_orders=True,
        enable_external_data=True,
        broker_provider="ibkr",
    )
    with patch(
        "app.market.live_prices.fetch_live_last_prices",
        new=AsyncMock(return_value={"AAPL": 999.0}),
    ) as fetch:
        prices, notes = await resolve_execution_prices(
            ["AAPL"],
            candidate_prices={"AAPL": 310.5},
            settings=settings,
        )
    assert prices == {"AAPL": 310.5}
    assert "reused_live_candidate_prices" in notes
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_execution_prices_ignores_stub_candidates_when_live_required() -> None:
    settings = Settings(
        enable_broker_orders=True,
        enable_external_data=True,
        enable_market_data_collection=True,
        broker_provider="ibkr",
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
    settings = Settings(
        enable_broker_orders=True,
        enable_external_data=True,
        broker_provider="ibkr",
    )
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


def test_get_market_data_provider_forces_ibkr_when_live_required_stub_request() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_broker_orders=True,
            enable_external_data=True,
            enable_market_data_collection=True,
            market_data_provider="stub",
            broker_provider="ibkr",
        ),
    ):
        provider = get_market_data_provider("stub")
    assert isinstance(provider, IbkrMarketDataProvider)


def test_get_market_data_provider_forces_ibkr_when_broker_is_ibkr() -> None:
    with patch(
        "app.collectors.market_data.get_settings",
        return_value=Settings(
            enable_broker_orders=True,
            enable_external_data=True,
            enable_market_data_collection=True,
            market_data_provider="stub",
            broker_provider="ibkr",
        ),
    ):
        provider = get_market_data_provider("stub")
    assert isinstance(provider, IbkrMarketDataProvider)


@pytest.mark.asyncio
async def test_fetch_live_last_prices_forwards_con_ids() -> None:
    settings = Settings(
        enable_broker_orders=True,
        enable_external_data=True,
        enable_market_data_collection=True,
        market_data_provider="stub",
        broker_provider="mock",
    )

    class _Prov:
        name = "ibkr"

        async def fetch_quotes(self, symbols, *, con_ids=None):  # noqa: ANN001
            self.seen = {"symbols": symbols, "con_ids": con_ids}
            from datetime import UTC, datetime

            from app.collectors.base import RawMarketQuote

            return [
                RawMarketQuote(
                    symbol="BHP",
                    as_of=datetime.now(UTC),
                    provider="ibkr",
                    last=41.25,
                )
            ]

    prov = _Prov()
    with patch(
        "app.collectors.market_data.get_market_data_provider",
        return_value=prov,
    ):
        prices = await fetch_live_last_prices(
            ["BHP"], settings=settings, con_ids={"BHP": 42}
        )
    assert prices == {"BHP": 41.25}
    assert prov.seen["con_ids"] == {"BHP": 42}
