"""IBKR broker adapter unit tests (no Gateway required)."""

from __future__ import annotations

import os

import pytest

from app.brokers.errors import BrokerError
from app.brokers.factory import get_broker
from app.brokers.ibkr import IbkrBroker, _map_status
from app.brokers.base import OrderStatus
from app.core.config import Settings, clear_settings_cache


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_map_ibkr_order_status() -> None:
    assert _map_status("Filled") == OrderStatus.FILLED
    assert _map_status("Submitted") == OrderStatus.ACCEPTED
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
