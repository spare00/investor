"""Broker package."""

from app.brokers.base import OrderRequest, OrderResult, OrderSide, OrderStatus
from app.brokers.errors import BrokerError
from app.brokers.factory import disconnect_broker, get_broker
from app.brokers.mock import MockBroker

# Compatibility alias used by older tests / docs.
SimulatedBroker = MockBroker

__all__ = [
    "BrokerError",
    "MockBroker",
    "OrderRequest",
    "OrderResult",
    "OrderSide",
    "OrderStatus",
    "SimulatedBroker",
    "disconnect_broker",
    "get_broker",
]
