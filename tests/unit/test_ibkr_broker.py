"""IBKR broker adapter unit tests (no Gateway required)."""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.brokers.base import OrderRequest, OrderSide, OrderStatus
from app.brokers.errors import BrokerError
from app.brokers.factory import get_broker
from app.brokers.ibkr import IbkrBroker, _map_status
from app.core.config import Settings, clear_settings_cache


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_map_ibkr_order_status() -> None:
    assert _map_status("Filled") == OrderStatus.FILLED
    assert _map_status("Submitted") == OrderStatus.ACCEPTED
    assert _map_status("ValidationError") == OrderStatus.REJECTED
    assert _map_status("Cancelled") == OrderStatus.CANCELED
    assert _map_status("PartiallyFilled") == OrderStatus.PARTIAL


def test_factory_ibkr_without_connection_uses_mock() -> None:
    settings = Settings(
        broker_provider="ibkr",
        broker_environment="paper",
        enable_broker_connection=False,
        enable_live_trading=False,
        app_env="development",
    )
    broker = get_broker(settings)
    assert broker.__class__.__name__ == "MockBroker"


class _Contract:
    def __init__(self, *, currency: str = "AUD", exchange: str = "ASX") -> None:
        self.symbol = "CBA"
        self.primaryExchange = exchange
        self.exchange = exchange
        self.currency = currency
        self.conId = 11


class _Status:
    def __init__(self) -> None:
        self.status = "PreSubmitted"
        self.filled = 0
        self.remaining = 326
        self.avgFillPrice = 0
        self.orderId = 42
        self.permId = 99
        self.whyHeld = ""


class _Trade:
    def __init__(self, order: Any, contract: _Contract) -> None:
        self.order = order
        self.contract = contract
        self.orderStatus = _Status()


class _FakeIB:
    def __init__(self) -> None:
        self.placed: list[tuple[Any, Any]] = []

    def reqMarketDataType(self, _kind: int) -> None:
        return None

    def placeOrder(self, contract: Any, order: Any) -> _Trade:
        self.placed.append((contract, order))
        return _Trade(order, contract)


def _paper_broker() -> IbkrBroker:
    return IbkrBroker(
        Settings(
            broker_provider="ibkr",
            broker_environment="paper",
            enable_broker_connection=True,
            enable_live_trading=False,
            ibkr_port=4002,
            app_env="development",
        )
    )


def _install_fake_gateway(
    broker: IbkrBroker,
    ib: _FakeIB,
    contract: _Contract,
    *,
    tape: dict[str, float | None] | None = None,
) -> None:
    async def connected() -> _FakeIB:
        return ib

    async def qualify(*_args: Any, **_kwargs: Any) -> _Contract:
        return contract

    async def wait(_ib: Any, trade: _Trade, *, seconds: float = 3.0) -> Any:
        del seconds
        return broker._trade_to_result(trade)

    async def snapshot(_ib: Any, _contract: Any) -> dict[str, float | None]:
        return tape or {"last": None, "bid": None, "ask": None}

    broker._ensure_connected = connected  # type: ignore[method-assign]
    broker._qualify_stock = qualify  # type: ignore[method-assign]
    broker._wait_trade = wait  # type: ignore[method-assign]
    broker._snapshot_tape = snapshot  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_au_protective_stop_reaches_ib_as_stop() -> None:
    broker = _paper_broker()
    ib = _FakeIB()
    _install_fake_gateway(broker, ib, _Contract())
    request = OrderRequest(
        symbol="CBA",
        side=OrderSide.SELL,
        qty=326,
        order_type="stop",
        stop_price=145.0,
        venue="AU",
        idempotency_key="protect-stop:cba",
        time_in_force="gtc",
    )

    result = await broker.submit_order(request)

    assert request.order_type == "stop"
    assert request.limit_price is None
    assert len(ib.placed) == 1
    order = ib.placed[0][1]
    assert order.orderType == "STP"
    assert float(order.auxPrice) == 145.0
    assert order.tif == "GTC"
    assert result.raw is not None
    assert result.raw["order_type"] == "stop"
    assert result.raw["limit_price"] is None


@pytest.mark.asyncio
async def test_au_stop_limit_keeps_trigger_and_limit() -> None:
    broker = _paper_broker()
    ib = _FakeIB()
    _install_fake_gateway(
        broker,
        ib,
        _Contract(),
        tape={"last": 150.0, "bid": 149.98, "ask": 150.02},
    )
    request = OrderRequest(
        symbol="CBA",
        side=OrderSide.SELL,
        qty=326,
        order_type="stp lmt",
        stop_price=145.0,
        limit_price=144.5,
        venue="AU",
        time_in_force="gtc",
    )

    await broker.submit_order(request)

    order = ib.placed[0][1]
    assert order.orderType == "STP LMT"
    assert float(order.auxPrice) == 145.0
    assert float(order.lmtPrice) == 144.5
    assert request.order_type == "stp lmt"
    assert request.limit_price == 144.5


@pytest.mark.asyncio
async def test_au_stop_without_trigger_is_rejected() -> None:
    broker = _paper_broker()
    ib = _FakeIB()
    _install_fake_gateway(broker, ib, _Contract())
    request = OrderRequest(
        symbol="CBA",
        side=OrderSide.SELL,
        qty=326,
        order_type="stop",
        venue="AU",
    )

    with pytest.raises(BrokerError, match="ibkr_stop_requires_price"):
        await broker.submit_order(request)
    assert ib.placed == []


@pytest.mark.asyncio
async def test_au_market_sell_still_becomes_through_quote_limit() -> None:
    broker = _paper_broker()
    ib = _FakeIB()
    _install_fake_gateway(
        broker,
        ib,
        _Contract(),
        tape={"last": 150.0, "bid": 149.98, "ask": 150.02},
    )
    request = OrderRequest(
        symbol="CBA",
        side=OrderSide.SELL,
        qty=326,
        order_type="market",
        venue="AU",
    )

    await broker.submit_order(request)

    order = ib.placed[0][1]
    assert order.orderType == "LMT"
    assert float(order.lmtPrice) == 146.23


def test_ibkr_refuses_live_looking_port() -> None:
    settings = Settings(
        broker_provider="ibkr",
        broker_environment="paper",
        enable_broker_connection=True,
        enable_live_trading=False,
        ibkr_port=4001,
        ibkr_allow_live_ports=False,
        app_env="development",
    )
    with pytest.raises(BrokerError, match="ibkr_port_looks_live"):
        IbkrBroker(settings)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_IBKR_PAPER_SMOKE_TESTS", "").lower() not in {"1", "true", "yes"},
    reason="Opt-in: requires IB Gateway paper on IBKR_PORT",
)
async def test_ibkr_paper_ping_opt_in() -> None:
    settings = Settings(
        broker_provider="ibkr",
        broker_environment="paper",
        enable_broker_connection=True,
        enable_live_trading=False,
        ibkr_host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        ibkr_port=int(os.environ.get("IBKR_PORT", "4002")),
        ibkr_client_id=int(os.environ.get("IBKR_CLIENT_ID", "17")),
        ibkr_account=os.environ.get("IBKR_ACCOUNT", ""),
        app_env="development",
    )
    broker = IbkrBroker(settings)
    try:
        out = await broker.ping()
        assert out["connected"] is True
        assert out["account"]["equity"] is not None
    finally:
        await broker.disconnect()
