"""Broker factory — explicit provider selection with safe defaults."""

from __future__ import annotations

from app.brokers.base import BrokerClient
from app.brokers.errors import BrokerError
from app.brokers.mock import MockBroker
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def get_broker(settings: Settings | None = None) -> BrokerClient:
    cfg = settings or get_settings()
    provider = (cfg.broker_provider or "mock").lower()

    if cfg.enable_live_trading or cfg.broker_environment.lower() == "live":
        # Phase 5 hard block — never construct a live broker here.
        raise BrokerError("live_trading_blocked_phase5")

    if provider == "mock" or cfg.app_env.value == "test":
        return MockBroker(seed=cfg.mock_broker_seed, starting_cash=cfg.starting_cash)

    if provider == "alpaca":
        if not cfg.enable_broker_connection:
            logger.warning("alpaca_requested_but_connection_disabled_using_mock")
            return MockBroker(seed=cfg.mock_broker_seed, starting_cash=cfg.starting_cash)
        if cfg.broker_environment.lower() != "paper":
            raise BrokerError("alpaca_requires_paper_environment")
        if not cfg.alpaca_api_key or not cfg.alpaca_api_secret:
            raise BrokerError("alpaca_credentials_missing")
        from app.brokers.alpaca import AlpacaBroker

        return AlpacaBroker(cfg)

    raise BrokerError(f"unknown_broker_provider:{provider}")
