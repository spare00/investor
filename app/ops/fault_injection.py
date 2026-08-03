"""Fault injection framework for non-production environments."""

from __future__ import annotations

from enum import StrEnum

from app.core.config import AppEnv, Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class FaultKind(StrEnum):
    """Injectable faults — disabled by default; forbidden in production."""

    PROVIDER_TIMEOUT = "provider-timeout"
    PROVIDER_MALFORMED = "provider-malformed"
    PROVIDER_STALE = "provider-stale"
    PROVIDER_CONFLICT = "provider-conflict"
    PROVIDER_OUTAGE = "provider-outage"
    LLM_TIMEOUT = "llm-timeout"
    LLM_INVALID_JSON = "llm-invalid-json"
    LLM_FAILURE = "llm-failure"
    DATABASE_LATENCY = "database-latency"
    DATABASE_UNAVAILABLE = "database-unavailable"
    DATABASE_SLOW = "database-slow"
    LEASE_LOSS = "lease-loss"
    SCHEDULER_DELAY = "scheduler-delay"
    EVENT_QUEUE_BACKLOG = "event-queue-backlog"
    BROKER_TIMEOUT = "broker-timeout"
    BROKER_DISCONNECT = "broker-disconnect"
    BROKER_OUTAGE = "broker-outage"
    ORDER_RESPONSE_LOST = "order-response-lost"
    OUT_OF_ORDER_BROKER_EVENT = "out-of-order-broker-event"
    DUPLICATE_BROKER_EVENT = "duplicate-broker-event"
    PARTIAL_FILL_STALL = "partial-fill-stall"
    POSITION_DRIFT = "position-drift"
    CLOCK_SKEW = "clock-skew"
    SERVICE_RESTART = "service-restart"
    EMERGENCY_STOP = "emergency-stop"
    DATA_STALE = "data-stale"
    NETWORK_DELAY = "network-delay"
    WORKFLOW_FAILURE = "workflow-failure"
    RECONCILIATION_DRIFT = "reconciliation-drift"


class FaultInjectionError(RuntimeError):
    """Raised when fault injection is not permitted."""


class FaultInjectionFramework:
    """
    In-memory fault flags gated by ``enable_fault_injection`` and non-production env.

    Hard-rejects any use in production regardless of settings.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._active: set[FaultKind] = set()

    @property
    def enabled(self) -> bool:
        if self.settings.app_env == AppEnv.PRODUCTION:
            return False
        return bool(self.settings.enable_fault_injection)

    def _assert_allowed(self) -> None:
        if self.settings.app_env == AppEnv.PRODUCTION:
            raise FaultInjectionError("fault injection is forbidden in production")
        if not self.settings.enable_fault_injection:
            raise FaultInjectionError("fault injection is disabled (ENABLE_FAULT_INJECTION=false)")

    def inject(self, kind: FaultKind | str) -> None:
        self._assert_allowed()
        fault = FaultKind(kind)
        self._active.add(fault)
        logger.warning("fault_injected", fault=fault.value)

    def check(self, kind: FaultKind | str) -> bool:
        if not self.enabled:
            return False
        return FaultKind(kind) in self._active

    def clear(self, kind: FaultKind | str | None = None) -> None:
        if kind is None:
            self._active.clear()
            return
        self._active.discard(FaultKind(kind))

    def active_faults(self) -> list[str]:
        return sorted(f.value for f in self._active)
