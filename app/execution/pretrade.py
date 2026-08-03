"""Pre-trade risk validator (deterministic, post-intent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.brokers.models import PretradeStatus
from app.core.config import Settings, get_settings
from app.execution.safety_controls import TradingControls, trading_controls
from app.execution.sizing import SizingInput, size_position


@dataclass(slots=True)
class PretradeCheckResult:
    risk_check_id: str
    intent_id: str
    decision_id: str | None
    status: PretradeStatus
    requested_quantity: float
    approved_quantity: float
    requested_notional: float
    approved_notional: float
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    position_before: float = 0.0
    position_after: float = 0.0
    cash_before: float = 0.0
    cash_after: float = 0.0
    gross_exposure_before: float = 0.0
    gross_exposure_after: float = 0.0
    risk_amount: float = 0.0
    stop_distance: float = 0.0
    calculation_version: str = "pretrade_v1"
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_check_id": self.risk_check_id,
            "intent_id": self.intent_id,
            "decision_id": self.decision_id,
            "status": self.status.value,
            "requested_quantity": self.requested_quantity,
            "approved_quantity": self.approved_quantity,
            "requested_notional": self.requested_notional,
            "approved_notional": self.approved_notional,
            "violations": self.violations,
            "warnings": self.warnings,
            "risk_amount": self.risk_amount,
            "stop_distance": self.stop_distance,
            "calculation_version": self.calculation_version,
            "as_of": self.as_of.isoformat(),
        }


class PretradeRiskValidator:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        controls: TradingControls | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.controls = controls or trading_controls

    def validate(
        self,
        *,
        intent_id: str,
        decision_id: str | None,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_price: float | None,
        equity: float,
        cash: float,
        buying_power: float,
        gross_exposure: float,
        position_qty: float,
        data_quality_score: float,
        quote_age_seconds: float | None,
        spread_bps: float | None,
        hard_vetoes: list[str] | None = None,
        asset_tradable: bool = True,
        market_open: bool = True,
        account_blocked: bool = False,
        duplicate_client_order: bool = False,
        conflicting_open_order: bool = False,
        decision_expired: bool = False,
        open_positions: int = 0,
    ) -> PretradeCheckResult:
        rid = str(uuid4())
        violations: list[str] = []
        warnings: list[str] = []
        requested_notional = quantity * entry_price

        snap = self.controls.snapshot()
        if snap.state.value == "emergency_stop":
            return self._blocked(
                rid, intent_id, decision_id, quantity, requested_notional, ["emergency_stop"], equity, cash, gross_exposure, position_qty
            )
        if snap.state.value == "paused":
            return self._blocked(
                rid, intent_id, decision_id, quantity, requested_notional, ["system_paused"], equity, cash, gross_exposure, position_qty
            )
        if hard_vetoes:
            return self._blocked(
                rid, intent_id, decision_id, quantity, requested_notional, [f"hard_veto:{v}" for v in hard_vetoes], equity, cash, gross_exposure, position_qty
            )
        if decision_expired:
            violations.append("decision_expired")
        if account_blocked:
            violations.append("account_blocked")
        if not market_open and not self.settings.enable_extended_hours_orders:
            violations.append("market_closed")
        if not asset_tradable:
            violations.append("asset_not_tradable")
        if duplicate_client_order:
            violations.append("duplicate_client_order")
        if conflicting_open_order:
            violations.append("conflicting_open_order")
        if data_quality_score < self.settings.data_quality_hard_fail_threshold:
            violations.append("data_quality_hard_fail")
        elif data_quality_score < self.settings.data_quality_warning_threshold:
            warnings.append("data_quality_warning")
        if quote_age_seconds is not None and quote_age_seconds > self.settings.latest_quote_max_age_seconds * 20:
            violations.append("stale_quote")
        if spread_bps is not None and spread_bps > self.settings.max_order_spread_bps:
            violations.append("spread_too_wide")
        if open_positions >= self.settings.max_open_positions and side.lower() == "buy" and position_qty <= 0:
            violations.append("max_open_positions")
        if side.lower() in {"sell"} and quantity > abs(position_qty) + 1e-9 and not self.settings.enable_short_selling:
            # closing only
            if position_qty <= 0:
                violations.append("short_selling_disabled")

        if stop_price is None and side.lower() == "buy":
            violations.append("stop_required")
            return self._blocked(
                rid, intent_id, decision_id, quantity, requested_notional, violations, equity, cash, gross_exposure, position_qty
            )

        sizing = size_position(
            SizingInput(
                portfolio_equity=equity,
                risk_per_trade_pct=self.settings.risk_per_trade_pct,
                entry_price=entry_price,
                stop_price=float(stop_price or 0),
                max_position_pct=self.settings.max_position_pct,
                available_buying_power=buying_power,
                min_cash_pct=self.settings.min_cash_pct,
                existing_symbol_qty=position_qty,
                fractionable=True,
                side=side,
            )
        )
        if not sizing.approved:
            violations.append(sizing.reason or "sizing_rejected")

        approved_qty = min(quantity, sizing.quantity) if sizing.approved else 0.0
        if violations:
            return PretradeCheckResult(
                risk_check_id=rid,
                intent_id=intent_id,
                decision_id=decision_id,
                status=PretradeStatus.REJECTED if "emergency_stop" not in violations else PretradeStatus.SYSTEM_BLOCKED,
                requested_quantity=quantity,
                approved_quantity=0.0,
                requested_notional=requested_notional,
                approved_notional=0.0,
                violations=violations,
                warnings=warnings,
                position_before=position_qty,
                cash_before=cash,
                gross_exposure_before=gross_exposure,
                risk_amount=sizing.risk_amount,
                stop_distance=sizing.stop_distance,
            )

        status = PretradeStatus.APPROVED
        if approved_qty + 1e-9 < quantity:
            status = PretradeStatus.APPROVED_WITH_REDUCTION
            warnings.append("quantity_reduced_by_sizing")
        if self.settings.require_manual_order_approval:
            status = PretradeStatus.REQUIRES_MANUAL_APPROVAL

        signed = approved_qty if side.lower() == "buy" else -approved_qty
        return PretradeCheckResult(
            risk_check_id=rid,
            intent_id=intent_id,
            decision_id=decision_id,
            status=status,
            requested_quantity=quantity,
            approved_quantity=approved_qty,
            requested_notional=requested_notional,
            approved_notional=approved_qty * entry_price,
            violations=[],
            warnings=warnings,
            position_before=position_qty,
            position_after=position_qty + signed,
            cash_before=cash,
            cash_after=cash - (approved_qty * entry_price if side.lower() == "buy" else -approved_qty * entry_price),
            gross_exposure_before=gross_exposure,
            gross_exposure_after=gross_exposure + approved_qty * entry_price,
            risk_amount=sizing.risk_amount,
            stop_distance=sizing.stop_distance,
        )

    def _blocked(
        self,
        rid: str,
        intent_id: str,
        decision_id: str | None,
        qty: float,
        notional: float,
        violations: list[str],
        equity: float,
        cash: float,
        gross: float,
        pos: float,
    ) -> PretradeCheckResult:
        status = (
            PretradeStatus.SYSTEM_BLOCKED
            if any(v.startswith("emergency") or v.startswith("system_") for v in violations)
            else PretradeStatus.REJECTED
        )
        return PretradeCheckResult(
            risk_check_id=rid,
            intent_id=intent_id,
            decision_id=decision_id,
            status=status,
            requested_quantity=qty,
            approved_quantity=0.0,
            requested_notional=notional,
            approved_notional=0.0,
            violations=violations,
            position_before=pos,
            cash_before=cash,
            gross_exposure_before=gross,
        )
