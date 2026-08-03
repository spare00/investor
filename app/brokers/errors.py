"""Shared broker exceptions."""


class BrokerError(Exception):
    """Broker API failure — callers must fail closed."""
