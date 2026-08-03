"""Order intent builder + execution orchestration (Phase 5)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.factory import get_broker
from app.brokers.errors import BrokerError
from app.brokers.models import (
    ApprovalStatus,
    ExitPolicy,
    IntentStatus,
    IntentType,
    InternalOrderState,
    PretradeStatus,
    assert_order_transition,
)
from app.brokers.base import OrderRequest, OrderSide
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.pretrade import PretradeCheckResult, PretradeRiskValidator
from app.execution.safety_controls import TradingControls, trading_controls
from app.execution.validation import ExecutionValidator, ValidatedOrderIntent
from app.models import Order, OrderApproval, OrderIntent, PretradeRiskCheck
from app.risk import PortfolioRiskView
from app.schemas.cio import CIODecision

logger = get_logger(__name__)


def make_client_order_id(
    *,
    workflow_run_id: str | None,
    decision_id: str,
    intent_id: str,
    symbol: str,
    side: str,
    attempt: int = 1,
) -> str:
    base = f"{workflow_run_id or 'wf'}|{decision_id}|{intent_id}|{symbol}|{side}|{attempt}"
    digest = hashlib.sha256(base.encode()).hexdigest()[:24]
    return f"inv-{digest}"[:48]


class ExecutionService:
    """CIO Decision → Intent → Pretrade → Approval → (optional) Broker submit."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        controls: TradingControls | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.controls = controls or trading_controls
        self.pretrade = PretradeRiskValidator(settings=self.settings, controls=self.controls)
        self._broker = None

    @property
    def broker(self) -> Any:
        if self._broker is None:
            self._broker = get_broker(self.settings)
        return self._broker

    async def build_intents_from_decision(
        self,
        decision: CIODecision,
        *,
        portfolio: PortfolioRiskView,
        latest_prices: dict[str, float],
        data_quality_score: float = 1.0,
        workflow_id: UUID | None = None,
    ) -> list[OrderIntent]:
        validation = ExecutionValidator(settings=self.settings, controls=self.controls).validate(
            decision,
            portfolio=portfolio,
            latest_prices=latest_prices,
            data_quality_score=data_quality_score,
            workflow_id=str(workflow_id) if workflow_id else None,
        )
        created: list[OrderIntent] = []
        for v in validation.intents:
            intent_type = IntentType.OPEN_LONG if v.side == "buy" else IntentType.CLOSE_LONG
            if not self.settings.enable_short_selling and v.side == "sell":
                # treat as reduce/close long only
                intent_type = IntentType.CLOSE_LONG
            intent = OrderIntent(
                id=uuid4(),
                decision_id=UUID(v.decision_id) if _is_uuid(v.decision_id) else None,
                workflow_run_id=workflow_id,
                symbol=v.symbol.upper(),
                intent_type=intent_type.value,
                side=v.side,
                quantity=v.quantity,
                entry_price=v.limit_price,
                stop_price=v.stop_price,
                status=IntentStatus.CREATED.value,
                client_order_id=None,
                thesis=v.thesis,
                exit_policy={
                    "stop_loss": v.stop_price,
                    "overnight_allowed": False,
                    "closing_policy": "CLOSE_INTRADAY_ONLY",
                    "protection_submitted": False,
                },
                metadata_json={
                    "order_type": v.order_type,
                    "idempotency_key_seed": v.idempotency_key,
                    "validation_rejections": validation.rejections,
                },
            )
            self.session.add(intent)
            created.append(intent)
        await self.session.flush()
        return created

    async def validate_intent(
        self,
        intent_id: UUID,
        *,
        equity: float,
        cash: float,
        buying_power: float,
        gross_exposure: float,
        position_qty: float,
        data_quality_score: float = 1.0,
        quote_age_seconds: float | None = 0.0,
        spread_bps: float | None = 10.0,
        hard_vetoes: list[str] | None = None,
        market_open: bool = True,
        asset_tradable: bool = True,
    ) -> PretradeCheckResult:
        intent = await self.session.get(OrderIntent, intent_id)
        if intent is None:
            raise ValueError("intent_not_found")
        intent.status = IntentStatus.VALIDATING.value
        entry = float(intent.entry_price or 0)
        result = self.pretrade.validate(
            intent_id=str(intent.id),
            decision_id=str(intent.decision_id) if intent.decision_id else None,
            symbol=intent.symbol,
            side=intent.side,
            quantity=float(intent.quantity or 0),
            entry_price=entry,
            stop_price=float(intent.stop_price) if intent.stop_price is not None else None,
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            gross_exposure=gross_exposure,
            position_qty=position_qty,
            data_quality_score=data_quality_score,
            quote_age_seconds=quote_age_seconds,
            spread_bps=spread_bps,
            hard_vetoes=hard_vetoes,
            asset_tradable=asset_tradable,
            market_open=market_open,
            account_blocked=False,
            decision_expired=bool(
                intent.expires_at and intent.expires_at < datetime.now(UTC)
            ),
        )
        self.session.add(
            PretradeRiskCheck(
                id=UUID(result.risk_check_id) if _is_uuid(result.risk_check_id) else uuid4(),
                intent_id=intent.id,
                decision_id=intent.decision_id,
                status=result.status.value,
                payload=result.to_dict(),
            )
        )
        intent.risk_check_id = UUID(result.risk_check_id) if _is_uuid(result.risk_check_id) else None
        intent.approved_quantity = result.approved_quantity
        if result.status in {PretradeStatus.REJECTED, PretradeStatus.SYSTEM_BLOCKED}:
            intent.status = IntentStatus.RISK_REJECTED.value
        elif result.status == PretradeStatus.REQUIRES_MANUAL_APPROVAL:
            intent.status = IntentStatus.PENDING_APPROVAL.value
            self.session.add(
                OrderApproval(
                    id=uuid4(),
                    intent_id=intent.id,
                    status=ApprovalStatus.PENDING_APPROVAL.value,
                    expires_at=datetime.now(UTC)
                    + timedelta(minutes=self.settings.order_approval_expiry_minutes),
                )
            )
        elif result.status in {PretradeStatus.APPROVED, PretradeStatus.APPROVED_WITH_REDUCTION}:
            if self.settings.require_manual_order_approval:
                intent.status = IntentStatus.PENDING_APPROVAL.value
            else:
                intent.status = IntentStatus.APPROVED.value
        await self.session.flush()
        return result

    async def approve_intent(self, intent_id: UUID, *, actor: str = "operator") -> OrderIntent:
        intent = await self._require_intent(intent_id)
        if intent.status != IntentStatus.PENDING_APPROVAL.value:
            raise ValueError(f"approve_not_allowed_from:{intent.status}")
        approval = await self._latest_approval(intent_id)
        if approval and approval.expires_at:
            exp = approval.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp < datetime.now(UTC):
                approval.status = ApprovalStatus.EXPIRED.value
                intent.status = IntentStatus.EXPIRED.value
                await self.session.flush()
                raise ValueError("approval_expired")
        if approval:
            approval.status = ApprovalStatus.APPROVED.value
            approval.acted_by = actor
            approval.acted_at = datetime.now(UTC)
            intent.approval_id = approval.id
        intent.status = IntentStatus.APPROVED.value
        await self.session.flush()
        return intent

    async def reject_intent(self, intent_id: UUID, *, actor: str = "operator", reason: str = "") -> OrderIntent:
        intent = await self._require_intent(intent_id)
        approval = await self._latest_approval(intent_id)
        if approval:
            approval.status = ApprovalStatus.REJECTED.value
            approval.acted_by = actor
            approval.acted_at = datetime.now(UTC)
            approval.reason = reason
        intent.status = IntentStatus.REJECTED.value
        await self.session.flush()
        return intent

    async def submit_intent(self, intent_id: UUID, *, attempt: int = 1) -> Order | None:
        """Submit approved intent to broker — gated by ENABLE_BROKER_ORDERS."""
        intent = await self._require_intent(intent_id)
        await self.session.refresh(intent)
        if self.controls.snapshot().state.value == "emergency_stop":
            raise BrokerError("emergency_stop_active")
        if not self.settings.enable_broker_orders:
            raise BrokerError("enable_broker_orders_false")
        if not self.settings.enable_broker_connection and self.settings.broker_provider != "mock":
            raise BrokerError("broker_connection_disabled")
        if intent.status not in {
            IntentStatus.APPROVED.value,
            IntentStatus.SUBMITTED.value,
            IntentStatus.SUBMITTING.value,
        }:
            raise ValueError(f"submit_not_allowed_from:{intent.status}")

        client_order_id = make_client_order_id(
            workflow_run_id=str(intent.workflow_run_id) if intent.workflow_run_id else None,
            decision_id=str(intent.decision_id or ""),
            intent_id=str(intent.id),
            symbol=intent.symbol,
            side=intent.side,
            attempt=attempt,
        )
        # Idempotency: existing order with same client id (before age checks / mutations)
        existing = (
            await self.session.execute(select(Order).where(Order.idempotency_key == client_order_id))
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        if intent.status != IntentStatus.APPROVED.value:
            raise ValueError(f"submit_not_allowed_from:{intent.status}")

        # Final revalidation window (normalize naive SQLite timestamps)
        updated = intent.updated_at
        if updated is not None:
            if getattr(updated, "tzinfo", None) is None:
                updated = updated.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - updated).total_seconds()
            if age > self.settings.order_submission_revalidation_max_age_seconds:
                meta = dict(intent.metadata_json or {})
                meta["revalidation_warning"] = "approval_aged"
                intent.metadata_json = meta

        # Check broker for same client order id (timeout recovery)
        if hasattr(self.broker, "get_order_by_client_id"):
            remote = await self.broker.get_order_by_client_id(client_order_id)
            if remote is not None:
                order = Order(
                    id=uuid4(),
                    broker_order_id=remote.broker_order_id,
                    idempotency_key=client_order_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    qty=float(intent.approved_quantity or intent.quantity or 0),
                    order_type=str((intent.metadata_json or {}).get("order_type") or "limit"),
                    limit_price=intent.entry_price,
                    stop_price=intent.stop_price,
                    status=remote.status.value,
                    decision_id=intent.decision_id,
                    submitted_at=remote.submitted_at,
                    raw_payload={"recovered": True, "intent_id": str(intent.id)},
                )
                self.session.add(order)
                intent.status = IntentStatus.SUBMITTED.value
                intent.client_order_id = client_order_id
                await self.session.flush()
                return order

        intent.status = IntentStatus.SUBMITTING.value
        intent.client_order_id = client_order_id
        qty = float(intent.approved_quantity or intent.quantity or 0)
        if qty <= 0:
            intent.status = IntentStatus.FAILED.value
            await self.session.flush()
            raise BrokerError("quantity_zero")

        assert_order_transition(InternalOrderState.APPROVED, InternalOrderState.SUBMITTING)
        order_row = Order(
            id=uuid4(),
            broker_order_id=None,
            idempotency_key=client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            qty=qty,
            order_type=str((intent.metadata_json or {}).get("order_type") or "limit"),
            limit_price=intent.entry_price,
            stop_price=intent.stop_price,
            status=InternalOrderState.SUBMITTING.value,
            decision_id=intent.decision_id,
            submitted_at=datetime.now(UTC),
            raw_payload={"intent_id": str(intent.id), "state": InternalOrderState.SUBMITTING.value},
        )
        self.session.add(order_row)
        await self.session.flush()

        try:
            result = await self.broker.submit_order(
                OrderRequest(
                    symbol=intent.symbol,
                    side=OrderSide.BUY if intent.side.lower() == "buy" else OrderSide.SELL,
                    qty=qty,
                    order_type=order_row.order_type,
                    limit_price=intent.entry_price,
                    stop_price=intent.stop_price,
                    idempotency_key=client_order_id,
                )
            )
            order_row.broker_order_id = result.broker_order_id
            order_row.status = result.status.value
            order_row.raw_payload = {
                "intent_id": str(intent.id),
                "broker": result.raw,
                "state": InternalOrderState.SUBMITTED.value,
            }
            intent.status = IntentStatus.SUBMITTED.value
            # Exit policy: record stop; protection not auto-submitted unless configured
            policy = ExitPolicy(
                stop_loss=intent.stop_price,
                overnight_allowed=False,
                protection_submitted=False,
            )
            meta = dict(intent.metadata_json or {})
            meta["exit_policy"] = policy.model_dump()
            intent.metadata_json = meta
            await self.session.flush()
            return order_row
        except TimeoutError:
            order_row.status = InternalOrderState.UNKNOWN.value
            intent.status = IntentStatus.FAILED.value
            order_row.raw_payload = {
                "intent_id": str(intent.id),
                "state": InternalOrderState.RECONCILIATION_REQUIRED.value,
                "error": "timeout",
            }
            await self.session.flush()
            raise
        except Exception as exc:  # noqa: BLE001
            order_row.status = InternalOrderState.FAILED.value
            intent.status = IntentStatus.FAILED.value
            order_row.raw_payload = {"intent_id": str(intent.id), "error": str(exc)[:300]}
            await self.session.flush()
            raise

    async def list_intents(self) -> list[OrderIntent]:
        return list((await self.session.execute(select(OrderIntent))).scalars().all())

    async def _require_intent(self, intent_id: UUID) -> OrderIntent:
        intent = await self.session.get(OrderIntent, intent_id)
        if intent is None:
            raise ValueError("intent_not_found")
        return intent

    async def _latest_approval(self, intent_id: UUID) -> OrderApproval | None:
        return (
            await self.session.execute(
                select(OrderApproval)
                .where(OrderApproval.intent_id == intent_id)
                .order_by(OrderApproval.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


def _is_uuid(value: str | None) -> bool:
    if not value:
        return False
    try:
        UUID(str(value))
        return True
    except ValueError:
        return False
