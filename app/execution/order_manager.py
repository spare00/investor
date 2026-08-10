"""Order persistence and broker submission (paper)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import BrokerClient, OrderRequest, OrderSide, OrderStatus
from app.brokers.errors import BrokerError
from app.brokers.factory import get_broker
from app.brokers.models import InternalOrderState
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.safety_controls import TradingControls, trading_controls
from app.execution.validation import ExecutionValidationResult, ValidatedOrderIntent
from app.market.venues import resolve_venue
from app.models import Execution, Order
from app.storage.repositories import SystemEventRepository

logger = get_logger(__name__)

_BROKER_STATUS_TO_INTERNAL = {
    OrderStatus.NEW: InternalOrderState.ACCEPTED,
    OrderStatus.ACCEPTED: InternalOrderState.ACCEPTED,
    OrderStatus.PARTIAL: InternalOrderState.PARTIALLY_FILLED,
    OrderStatus.FILLED: InternalOrderState.FILLED,
    OrderStatus.CANCELED: InternalOrderState.CANCELLED,
    OrderStatus.REJECTED: InternalOrderState.REJECTED,
}


def _internal_status(broker_status: OrderStatus) -> str:
    return _BROKER_STATUS_TO_INTERNAL.get(broker_status, InternalOrderState.UNKNOWN).value


class OrderManager:
    def __init__(
        self,
        session: AsyncSession,
        *,
        broker: BrokerClient | None = None,
        settings: Settings | None = None,
        controls: TradingControls | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.broker = broker or get_broker(self.settings)
        self.controls = controls or trading_controls
        self.events = SystemEventRepository(session)

    async def seen_idempotency_keys(self) -> set[str]:
        result = await self.session.execute(select(Order.idempotency_key))
        return {row[0] for row in result.all()}

    async def submit_validated_intents(
        self,
        validation: ExecutionValidationResult,
        *,
        decision_id: UUID | None = None,
        workflow_id: UUID | None = None,
    ) -> list[Order]:
        if not validation.approved:
            await self.events.record(
                level="warning",
                event_type="order_submit_blocked_validation",
                message="Validation not approved — fail closed",
                context={"rejections": validation.rejections},
                workflow_id=workflow_id,
            )
            return []

        if not self.controls.is_new_order_allowed():
            snap = self.controls.snapshot()
            await self.events.record(
                level="error",
                event_type="order_submit_blocked_controls",
                message=f"Trading controls: {snap.state.value}",
                workflow_id=workflow_id,
            )
            return []

        if not self.settings.enable_broker_orders:
            await self.events.record(
                level="warning",
                event_type="order_submit_blocked_flag",
                message="ENABLE_BROKER_ORDERS=false",
                workflow_id=workflow_id,
            )
            return []

        if self.settings.require_manual_order_approval and not self.settings.enable_automated_execution:
            await self.events.record(
                level="warning",
                event_type="order_submit_blocked_manual_approval",
                message="Use ExecutionService approve+submit path",
                workflow_id=workflow_id,
            )
            return []

        if self.settings.enable_live_trading or self.settings.broker_environment.lower() == "live":
            await self.events.record(
                level="error",
                event_type="order_submit_blocked_live",
                message="live_trading_blocked_phase5",
                workflow_id=workflow_id,
            )
            return []

        created: list[Order] = []
        for intent in validation.intents:
            order = await self._submit_one(intent, decision_id=decision_id, workflow_id=workflow_id)
            if order is not None:
                created.append(order)
        return created

    async def _submit_one(
        self,
        intent: ValidatedOrderIntent,
        *,
        decision_id: UUID | None,
        workflow_id: UUID | None,
    ) -> Order | None:
        # DB-level idempotency
        existing = await self.session.execute(
            select(Order).where(Order.idempotency_key == intent.idempotency_key)
        )
        if existing.scalar_one_or_none() is not None:
            logger.info("order_idempotent_skip", key=intent.idempotency_key)
            return None

        row = Order(
            id=uuid4(),
            idempotency_key=intent.idempotency_key,
            symbol=intent.symbol,
            side=intent.side,
            qty=intent.quantity,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            stop_price=intent.stop_price,
            status="pending_submit",
            decision_id=decision_id or UUID(intent.decision_id),
            submitted_at=None,
            raw_payload={
                "thesis": intent.thesis,
                "venue": intent.venue or resolve_venue(self.settings).value,
                "con_id": intent.con_id,
            },
        )
        self.session.add(row)
        await self.session.flush()

        try:
            order_type = intent.order_type
            limit_price = intent.limit_price
            if order_type in {"limit", "stop_limit"} and limit_price is None:
                raise BrokerError(f"{intent.symbol}: limit order missing limit_price")
            venue = intent.venue or (row.raw_payload or {}).get("venue")
            con_id = intent.con_id or (row.raw_payload or {}).get("con_id")
            result = await self.broker.submit_order(
                OrderRequest(
                    symbol=intent.symbol,
                    side=OrderSide(intent.side),
                    qty=intent.quantity,
                    order_type=order_type,
                    limit_price=limit_price,
                    stop_price=intent.stop_price,
                    idempotency_key=intent.idempotency_key,
                    venue=str(venue) if venue else None,
                    con_id=int(con_id) if con_id else None,
                )
            )
        except TimeoutError as exc:
            row.status = "UNKNOWN"
            row.raw_payload = {
                **row.raw_payload,
                "error": "timeout",
                "state": "RECONCILIATION_REQUIRED",
            }
            await self.events.record(
                level="error",
                event_type="broker_submit_timeout",
                message=str(exc),
                context={"symbol": intent.symbol, "idempotency_key": intent.idempotency_key},
                workflow_id=workflow_id,
            )
            logger.exception("broker_submit_timeout", symbol=intent.symbol)
            # Do not resubmit — recover via client order id / reconciliation
            if hasattr(self.broker, "get_order_by_client_id"):
                remote = await self.broker.get_order_by_client_id(intent.idempotency_key)
                if remote is not None:
                    row.broker_order_id = remote.broker_order_id
                    row.status = _internal_status(remote.status)
                    row.submitted_at = remote.submitted_at
            return row
        except BrokerError as exc:
            row.status = InternalOrderState.REJECTED.value
            row.raw_payload = {**row.raw_payload, "error": str(exc)}
            await self.events.record(
                level="error",
                event_type="broker_submit_failed",
                message=str(exc),
                context={"symbol": intent.symbol, "idempotency_key": intent.idempotency_key},
                workflow_id=workflow_id,
            )
            # Expected reject (e.g. broker validation) — keep row rejected, continue batch.
            logger.error("broker_submit_failed", symbol=intent.symbol, error=str(exc)[:240])
            return row

        row.broker_order_id = result.broker_order_id
        row.status = _internal_status(result.status)
        row.submitted_at = result.submitted_at
        row.raw_payload = {**row.raw_payload, "broker": result.raw or {}}

        if result.status == OrderStatus.FILLED and result.avg_fill_price is not None:
            self.session.add(
                Execution(
                    id=uuid4(),
                    order_id=row.id,
                    symbol=intent.symbol,
                    qty=result.filled_qty or intent.quantity,
                    price=result.avg_fill_price,
                    executed_at=datetime.now(UTC),
                    raw_payload=result.raw or {},
                )
            )
        await self.session.flush()
        logger.info(
            "order_submitted",
            symbol=intent.symbol,
            broker_order_id=row.broker_order_id,
            status=row.status,
        )
        return row

    async def cancel_order(self, order_id: UUID) -> Order:
        result = await self.session.execute(select(Order).where(Order.id == order_id))
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(f"order {order_id} not found")
        if row.broker_order_id:
            try:
                br = await self.broker.cancel_order(row.broker_order_id)
                row.status = br.status.value
            except BrokerError as exc:
                await self.events.record(
                    level="error",
                    event_type="broker_cancel_failed",
                    message=str(exc),
                    context={"order_id": str(order_id)},
                )
                raise
        else:
            row.status = OrderStatus.CANCELED.value
        await self.session.flush()
        return row

    async def emergency_cancel_all(self) -> dict[str, Any]:
        snap = self.controls.emergency_stop("api_emergency_stop")
        canceled = 0
        try:
            canceled = await self.broker.cancel_all_orders()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            await self.events.record(
                level="error",
                event_type="emergency_cancel_failed",
                message=str(exc),
            )
            # Still leave emergency stop engaged (fail closed for new orders)
            return {
                "state": snap.state.value,
                "canceled_count": 0,
                "error": str(exc),
            }
        return {
            "state": snap.state.value,
            "canceled_count": canceled,
            "canceled_open_orders": True,
        }

    async def list_orders(self, *, limit: int = 50) -> list[Order]:
        result = await self.session.execute(
            select(Order).order_by(Order.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def sync_statuses_from_broker(self) -> dict[str, Any]:
        """Refresh local open/pending orders from broker truth."""
        openish = {"new", "accepted", "partially_filled", "pending_submit", "pending_new"}
        result = await self.session.execute(
            select(Order).where(Order.status.in_(list(openish)))
        )
        rows = list(result.scalars().all())
        updated = 0
        errors = 0
        for row in rows:
            if not row.broker_order_id:
                continue
            try:
                br = await self.broker.get_order(row.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.warning(
                    "order_status_sync_failed",
                    broker_order_id=row.broker_order_id,
                    error=str(exc),
                )
                continue
            new_status = br.status.value
            if row.status != new_status:
                row.status = new_status
                row.raw_payload = {**(row.raw_payload or {}), "broker_sync": br.raw or {}}
                updated += 1
                if br.status == OrderStatus.FILLED and br.avg_fill_price is not None:
                    self.session.add(
                        Execution(
                            id=uuid4(),
                            order_id=row.id,
                            symbol=row.symbol,
                            qty=br.filled_qty or row.qty,
                            price=br.avg_fill_price,
                            executed_at=datetime.now(UTC),
                            raw_payload=br.raw or {},
                        )
                    )
        await self.session.flush()
        logger.info("orders_synced_from_broker", checked=len(rows), updated=updated, errors=errors)
        return {"checked": len(rows), "updated": updated, "errors": errors}
