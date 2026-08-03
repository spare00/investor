"""Readiness gate evaluation — explicit operator promotion only; LIVE always blocked."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.config import Settings, get_settings


class ReadinessGate(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    SIMULATION_READY = "SIMULATION_READY"
    PAPER_OBSERVE_READY = "PAPER_OBSERVE_READY"
    PAPER_MANUAL_READY = "PAPER_MANUAL_READY"
    PAPER_AUTOMATED_CANDIDATE = "PAPER_AUTOMATED_CANDIDATE"
    PAPER_AUTOMATED_APPROVED = "PAPER_AUTOMATED_APPROVED"
    LIVE_NOT_ALLOWED = "LIVE_NOT_ALLOWED"


@dataclass(slots=True)
class ReadinessCheck:
    name: str
    passed: bool
    detail: str
    required_for: ReadinessGate | None = None


class GateEvaluator:
    """Evaluate checks for a gate. Never auto-promotes. LIVE always blocked."""

    GATE_ORDER: list[ReadinessGate] = [
        ReadinessGate.DEVELOPMENT,
        ReadinessGate.SIMULATION_READY,
        ReadinessGate.PAPER_OBSERVE_READY,
        ReadinessGate.PAPER_MANUAL_READY,
        ReadinessGate.PAPER_AUTOMATED_CANDIDATE,
        ReadinessGate.PAPER_AUTOMATED_APPROVED,
        ReadinessGate.LIVE_NOT_ALLOWED,
    ]

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate(self, current_gate: ReadinessGate | str | None = None) -> dict[str, Any]:
        gate = ReadinessGate(current_gate or ReadinessGate.DEVELOPMENT)
        checks = self._build_checks(gate)
        required = [c for c in checks if c.required_for is None or c.required_for == gate]
        # Cumulative: all checks up to and including current gate that apply
        applicable = [
            c
            for c in checks
            if c.required_for is None
            or self.GATE_ORDER.index(c.required_for) <= self.GATE_ORDER.index(gate)
        ]
        passed = all(c.passed for c in applicable)
        return {
            "current_gate": gate.value,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "required_for": c.required_for.value if c.required_for else None,
                }
                for c in checks
            ],
            "all_required_passed": passed,
            "live_trading_allowed": False,
            "live_blocked_reason": "LIVE trading is not permitted in Phase 7 (LIVE_NOT_ALLOWED permanent)",
            "next_gate": self._next_gate(gate),
            "auto_promote": False,
            "operator_approval_required": True,
        }

    def _next_gate(self, gate: ReadinessGate) -> str | None:
        try:
            idx = self.GATE_ORDER.index(gate)
        except ValueError:
            return None
        if idx + 1 >= len(self.GATE_ORDER):
            return None
        nxt = self.GATE_ORDER[idx + 1]
        if nxt == ReadinessGate.LIVE_NOT_ALLOWED:
            return ReadinessGate.LIVE_NOT_ALLOWED.value
        return nxt.value

    def _build_checks(self, gate: ReadinessGate) -> list[ReadinessCheck]:
        cfg = self.settings
        return [
            ReadinessCheck(
                name="live_trading_disabled",
                passed=not cfg.enable_live_trading and not cfg.is_live_trading_allowed(),
                detail="ENABLE_LIVE_TRADING=false and dual-gate closed",
            ),
            ReadinessCheck(
                name="automated_execution_disabled_default",
                passed=not cfg.enable_automated_execution,
                detail="ENABLE_AUTOMATED_EXECUTION=false",
            ),
            ReadinessCheck(
                name="broker_orders_disabled_or_manual",
                passed=(not cfg.enable_broker_orders) or cfg.require_manual_order_approval,
                detail="Orders off or manual approval required",
            ),
            ReadinessCheck(
                name="fault_injection_disabled",
                passed=not cfg.enable_fault_injection,
                detail="ENABLE_FAULT_INJECTION=false for non-test ops",
                required_for=ReadinessGate.SIMULATION_READY,
            ),
            ReadinessCheck(
                name="simulation_fixtures_reproducible",
                passed=True,
                detail="Mock/fixture simulation path available",
                required_for=ReadinessGate.SIMULATION_READY,
            ),
            ReadinessCheck(
                name="broker_orders_inactive_for_observe",
                passed=not cfg.enable_broker_orders,
                detail="ENABLE_BROKER_ORDERS=false for PAPER_OBSERVE_READY",
                required_for=ReadinessGate.PAPER_OBSERVE_READY,
            ),
            ReadinessCheck(
                name="manual_approval_required",
                passed=cfg.require_manual_order_approval,
                detail="REQUIRE_MANUAL_ORDER_APPROVAL=true",
                required_for=ReadinessGate.PAPER_MANUAL_READY,
            ),
            ReadinessCheck(
                name="alerts_configured",
                passed=cfg.enable_alerts,
                detail="ENABLE_ALERTS=true",
                required_for=ReadinessGate.PAPER_MANUAL_READY,
            ),
            ReadinessCheck(
                name="min_performance_observations_config",
                passed=cfg.min_performance_observations >= 20,
                detail=f"MIN_PERFORMANCE_OBSERVATIONS={cfg.min_performance_observations}",
                required_for=ReadinessGate.PAPER_AUTOMATED_CANDIDATE,
            ),
            ReadinessCheck(
                name="human_approval_placeholder",
                passed=False,
                detail="Explicit operator approval required — not auto-granted",
                required_for=ReadinessGate.PAPER_AUTOMATED_APPROVED,
            ),
            ReadinessCheck(
                name="live_permanently_blocked",
                passed=True,
                detail="LIVE_NOT_ALLOWED — Phase 7 cannot enable live trading",
                required_for=ReadinessGate.LIVE_NOT_ALLOWED,
            ),
        ]
