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
        gate = ReadinessGate(current_gate or self.default_gate())
        checks = self._build_checks(gate)
        # Gate-specific checks apply to the evaluated gate only (MANUAL vs AUTOMATED
        # are alternate paper paths, not a strict cumulative ladder).
        applicable = [
            c for c in checks if c.required_for is None or c.required_for == gate
        ]
        # Always include earlier non-conflicting scaffold gates up through SIMULATION.
        scaffold = {
            ReadinessGate.DEVELOPMENT,
            ReadinessGate.SIMULATION_READY,
        }
        if gate not in scaffold:
            applicable = [
                c
                for c in checks
                if c.required_for is None
                or c.required_for in scaffold
                or c.required_for == gate
            ]
        # Deduplicate while preserving order
        seen: set[str] = set()
        uniq: list[ReadinessCheck] = []
        for c in applicable:
            if c.name in seen:
                continue
            seen.add(c.name)
            uniq.append(c)
        passed = all(c.passed for c in uniq)
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
            "live_blocked_reason": "LIVE trading is not permitted (LIVE_NOT_ALLOWED permanent)",
            "next_gate": self._next_gate(gate),
            "auto_promote": False,
            "operator_approval_required": True,
        }

    def default_gate(self) -> ReadinessGate:
        mode = (self.settings.intraday_operation_mode or "").upper()
        if mode == "PAPER_AUTOMATED":
            return ReadinessGate.PAPER_AUTOMATED_CANDIDATE
        if mode == "MANUAL_APPROVAL":
            return ReadinessGate.PAPER_MANUAL_READY
        if mode == "OBSERVE_ONLY":
            return ReadinessGate.PAPER_OBSERVE_READY
        return ReadinessGate.DEVELOPMENT

    def _next_gate(self, gate: ReadinessGate) -> str | None:
        try:
            idx = self.GATE_ORDER.index(gate)
        except ValueError:
            return None
        if idx + 1 >= len(self.GATE_ORDER):
            return None
        return self.GATE_ORDER[idx + 1].value

    def _build_checks(self, gate: ReadinessGate) -> list[ReadinessCheck]:
        cfg = self.settings
        paper_url_ok = (
            (cfg.broker_provider or "").lower() in {"mock", "ibkr"}
            and (cfg.broker_environment or "").lower() == "paper"
        )
        return [
            ReadinessCheck(
                name="live_trading_disabled",
                passed=not cfg.enable_live_trading and not cfg.is_live_trading_allowed(),
                detail="ENABLE_LIVE_TRADING=false and dual-gate closed",
            ),
            ReadinessCheck(
                name="paper_environment",
                passed=cfg.trading_mode.value in {"paper", "simulation"}
                and cfg.broker_environment.lower() == "paper",
                detail=f"mode={cfg.trading_mode.value} broker_env={cfg.broker_environment}",
            ),
            ReadinessCheck(
                name="fault_injection_disabled",
                passed=not cfg.enable_fault_injection,
                detail="ENABLE_FAULT_INJECTION=false for ops",
                required_for=ReadinessGate.SIMULATION_READY,
            ),
            ReadinessCheck(
                name="simulation_fixtures_reproducible",
                passed=True,
                detail="Mock/fixture simulation path available",
                required_for=ReadinessGate.SIMULATION_READY,
            ),
            ReadinessCheck(
                name="broker_connection_for_observe",
                passed=cfg.enable_broker_connection or cfg.broker_provider == "mock",
                detail="Broker read path available (connection or mock)",
                required_for=ReadinessGate.PAPER_OBSERVE_READY,
            ),
            ReadinessCheck(
                name="manual_mode_orders_gated",
                passed=(not cfg.enable_broker_orders) or cfg.require_manual_order_approval,
                detail="For MANUAL gate: orders off or manual brake on",
                required_for=ReadinessGate.PAPER_MANUAL_READY,
            ),
            ReadinessCheck(
                name="alerts_configured",
                passed=cfg.enable_alerts,
                detail="ENABLE_ALERTS=true",
                required_for=ReadinessGate.PAPER_MANUAL_READY,
            ),
            ReadinessCheck(
                name="paper_automated_cio_path",
                passed=(
                    cfg.enable_broker_orders
                    and cfg.enable_automated_execution
                    and not cfg.require_manual_order_approval
                    and not cfg.enable_live_trading
                    and paper_url_ok
                ),
                detail="CIO paper path armed: orders+automated on, manual brake off, Live off",
                required_for=ReadinessGate.PAPER_AUTOMATED_CANDIDATE,
            ),
            ReadinessCheck(
                name="intraday_paper_automated_mode",
                passed=(cfg.intraday_operation_mode or "").upper() == "PAPER_AUTOMATED",
                detail=f"INTRADAY_OPERATION_MODE={cfg.intraday_operation_mode}",
                required_for=ReadinessGate.PAPER_AUTOMATED_CANDIDATE,
            ),
            ReadinessCheck(
                name="hard_stops_armed_for_paper_auto",
                passed=bool(cfg.auto_execute_hard_stops),
                detail="AUTO_EXECUTE_HARD_STOPS=true required for unattended exits",
                required_for=ReadinessGate.PAPER_AUTOMATED_CANDIDATE,
            ),
            ReadinessCheck(
                name="force_close_armed_for_paper_auto",
                passed=bool(cfg.auto_execute_force_close),
                detail="AUTO_EXECUTE_FORCE_CLOSE=true required for closing-window exits",
                required_for=ReadinessGate.PAPER_AUTOMATED_CANDIDATE,
            ),
            ReadinessCheck(
                name="external_data_for_paper_auto",
                passed=bool(cfg.enable_external_data),
                detail="ENABLE_EXTERNAL_DATA=true — fixtures forbidden when execution is armed",
                required_for=ReadinessGate.PAPER_AUTOMATED_CANDIDATE,
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
                detail="LIVE_NOT_ALLOWED — cannot enable live trading",
                required_for=ReadinessGate.LIVE_NOT_ALLOWED,
            ),
        ]
