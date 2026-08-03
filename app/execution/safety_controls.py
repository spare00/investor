"""In-process trading state: pause, resume, emergency stop (fail-closed)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock


class TradingState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    EMERGENCY_STOP = "emergency_stop"


@dataclass
class TradingControlState:
    state: TradingState = TradingState.ACTIVE
    reason: str | None = None
    changed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    canceled_open_orders: bool = False


class TradingControls:
    """
    Process-local trading kill switch.

    Emergency Stop blocks all new orders and signals open-order cancellation.
    LLM agents never mutate this directly — only API/execution layer does.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._state = TradingControlState()

    def _copy(self) -> TradingControlState:
        return TradingControlState(
            state=self._state.state,
            reason=self._state.reason,
            changed_at=self._state.changed_at,
            canceled_open_orders=self._state.canceled_open_orders,
        )

    def snapshot(self) -> TradingControlState:
        with self._lock:
            return self._copy()

    def is_new_order_allowed(self) -> bool:
        return self.snapshot().state == TradingState.ACTIVE

    def pause(self, reason: str = "manual_pause") -> TradingControlState:
        with self._lock:
            self._state = TradingControlState(
                state=TradingState.PAUSED,
                reason=reason,
                changed_at=datetime.now(UTC),
                canceled_open_orders=False,
            )
            return self._copy()

    def resume(self, reason: str = "manual_resume") -> TradingControlState:
        with self._lock:
            if self._state.state == TradingState.EMERGENCY_STOP:
                # Emergency stop requires explicit clear_emergency.
                return self._copy()
            self._state = TradingControlState(
                state=TradingState.ACTIVE,
                reason=reason,
                changed_at=datetime.now(UTC),
                canceled_open_orders=False,
            )
            return self._copy()

    def emergency_stop(self, reason: str = "emergency_stop") -> TradingControlState:
        with self._lock:
            self._state = TradingControlState(
                state=TradingState.EMERGENCY_STOP,
                reason=reason,
                changed_at=datetime.now(UTC),
                canceled_open_orders=True,
            )
            return self._copy()

    def clear_emergency(self, reason: str = "emergency_cleared") -> TradingControlState:
        """Explicit dual-step recovery from emergency stop → paused (not active)."""
        with self._lock:
            self._state = TradingControlState(
                state=TradingState.PAUSED,
                reason=reason,
                changed_at=datetime.now(UTC),
                canceled_open_orders=False,
            )
            return self._copy()


# Singleton for API / execution layers within one process.
trading_controls = TradingControls()
