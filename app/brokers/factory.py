"""Broker factory — explicit provider selection with safe defaults."""

from __future__ import annotations

from app.brokers.base import BrokerClient
from app.brokers.errors import BrokerError
from app.brokers.mock import MockBroker
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# One IB Gateway clientId per process — concurrent IbkrBroker instances fight over the slot.
_IBKR_SINGLETON: BrokerClient | None = None


def get_broker(settings: Settings | None = None) -> BrokerClient:
    global _IBKR_SINGLETON
    cfg = settings or get_settings()
    provider = (cfg.broker_provider or "mock").lower()

    if cfg.enable_live_trading or cfg.broker_environment.lower() == "live":
        # Phase 5 hard block — never construct a live broker here.
        raise BrokerError("live_trading_blocked_phase5")

    if provider == "mock" or cfg.app_env.value == "test":
        return MockBroker(seed=cfg.mock_broker_seed, starting_cash=cfg.starting_cash)

    if provider == "ibkr":
        if not cfg.enable_broker_connection:
            logger.warning("ibkr_requested_but_connection_disabled_using_mock")
            return MockBroker(seed=cfg.mock_broker_seed, starting_cash=cfg.starting_cash)
        if cfg.broker_environment.lower() != "paper":
            raise BrokerError("ibkr_requires_paper_environment")
        from app.brokers.ibkr import IbkrBroker

        if _IBKR_SINGLETON is None or not isinstance(_IBKR_SINGLETON, IbkrBroker):
            _IBKR_SINGLETON = IbkrBroker(cfg)
        return _IBKR_SINGLETON

    raise BrokerError(f"unknown_broker_provider:{provider}")


async def disconnect_broker() -> None:
    """Drop the shared IBKR session (app shutdown / tests)."""
    global _IBKR_SINGLETON
    broker = _IBKR_SINGLETON
    _IBKR_SINGLETON = None
    if broker is not None and hasattr(broker, "disconnect"):
        await broker.disconnect()
