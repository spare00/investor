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


WORKING_ORDER_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "partially_filled",
        "pending_submit",
        "pending_new",
        InternalOrderState.SUBMITTING.value,
        InternalOrderState.SUBMITTED.value,
        InternalOrderState.ACCEPTED.value,
        InternalOrderState.PARTIALLY_FILLED.value,
        InternalOrderState.CANCEL_PENDING.value,
        InternalOrderState.REPLACE_PENDING.value,
    }
)

_DEAD_ORDER_STATUSES = frozenset(
    {
        InternalOrderState.CANCELLED.value,
        InternalOrderState.REJECTED.value,
        "cancelled",
        "canceled",
        "rejected",
    }
)

_FLATTEN_MARKERS = ("force-close:", "hard-stop:", ":sell:SELL", ":buy:BUY")
_PARTIAL_MARKERS = (":sell:PARTIAL_SELL", ":sell:REDUCE", ":buy:REDUCE")


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

    def _is_exit_intent(self, intent: ValidatedOrderIntent) -> bool:
        key = str(intent.idempotency_key or "")
        if any(m in key for m in (*_FLATTEN_MARKERS, *_PARTIAL_MARKERS)):
            return True
        thesis = str(intent.thesis or "").lower()
        return thesis.startswith("force_close") or thesis.startswith("hard_stop") or thesis.startswith("closing:")

    def _is_flatten_intent(self, intent: ValidatedOrderIntent) -> bool:
        key = str(intent.idempotency_key or "")
        thesis = str(intent.thesis or "").lower()
        return any(m in key for m in _FLATTEN_MARKERS) or thesis.startswith("force_close") or thesis.startswith("hard_stop")

    def _is_partial_exit(self, intent: ValidatedOrderIntent) -> bool:
        key = str(intent.idempotency_key or "")
        return any(m in key for m in _PARTIAL_MARKERS)

    async def _working_same_side(self, symbol: str, side: str) -> list[Order]:
        rows = list(
            (
                await self.session.execute(
                    select(Order).where(
                        Order.symbol == symbol.upper(),
                        Order.side == side.lower(),
                        Order.status.in_(list(WORKING_ORDER_STATUSES)),
                    )
                )
            ).scalars().all()
        )
        return rows

    async def cancel_working_for_symbol(self, symbol: str, *, side: str | None = None) -> int:
        """Cancel local+broker working orders for a symbol (optionally one side)."""
        q = select(Order).where(
            Order.symbol == symbol.upper(),
            Order.status.in_(list(WORKING_ORDER_STATUSES)),
        )
        if side:
            q = q.where(Order.side == side.lower())
        rows = list((await self.session.execute(q)).scalars().all())
        n = 0
        for row in rows:
            try:
                await self.cancel_order(row.id)
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cancel_working_failed",
                    symbol=symbol,
                    order_id=str(row.id),
                    error=str(exc)[:160],
                )
        return n

    async def _submit_one(
        self,
        intent: ValidatedOrderIntent,
        *,
        decision_id: UUID | None,
        workflow_id: UUID | None,
    ) -> Order | None:
        if self._is_exit_intent(intent):
            working = await self._working_same_side(intent.symbol, intent.side)
            if working and self._is_partial_exit(intent):
                logger.info(
                    "order_skip_working_exit",
                    symbol=intent.symbol,
                    working=len(working),
                    key=intent.idempotency_key,
                )
                return None
            if working and self._is_flatten_intent(intent):
                await self.cancel_working_for_symbol(intent.symbol, side=intent.side)

        existing = (
            await self.session.execute(
                select(Order).where(Order.idempotency_key == intent.idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            st = str(existing.status or "")
            if st in WORKING_ORDER_STATUSES:
                logger.info("order_idempotent_skip", key=intent.idempotency_key)
                return None
            if st == InternalOrderState.FILLED.value or st.lower() == "filled":
                logger.info("order_idempotent_filled", key=intent.idempotency_key)
                return None
            if st not in _DEAD_ORDER_STATUSES:
                logger.info("order_idempotent_skip", key=intent.idempotency_key, status=st)
                return None
            row = existing
            row.qty = intent.quantity
            row.order_type = intent.order_type
            row.limit_price = intent.limit_price
            row.stop_price = intent.stop_price
            row.status = "pending_submit"
            row.broker_order_id = None
            row.raw_payload = {
                **(row.raw_payload or {}),
                "thesis": intent.thesis,
                "venue": intent.venue or resolve_venue(self.settings).value,
                "con_id": intent.con_id,
                "resubmit": True,
            }
        else:
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
        raw = dict(result.raw or {})
        row.raw_payload = {**(row.raw_payload or {}), "broker": raw}
        if raw.get("order_type"):
            row.order_type = str(raw["order_type"])
        if raw.get("limit_price") is not None:
            try:
                row.limit_price = float(raw["limit_price"])
            except (TypeError, ValueError):
                pass

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
        # Include both InternalOrderState values and legacy lowercase broker strings.
        openish = {
            "new",
            "accepted",
            "partially_filled",
            "pending_submit",
            "pending_new",
            InternalOrderState.SUBMITTING.value,
            InternalOrderState.SUBMITTED.value,
            InternalOrderState.ACCEPTED.value,
            InternalOrderState.PARTIALLY_FILLED.value,
            InternalOrderState.CANCEL_PENDING.value,
            InternalOrderState.REPLACE_PENDING.value,
        }
        result = await self.session.execute(
            select(Order).where(Order.status.in_(list(openish)))
        )
        rows = list(result.scalars().all())
        updated = 0
        errors = 0
        missing = 0
        for row in rows:
            if not row.broker_order_id:
                continue
            try:
                br = await self.broker.get_order(row.broker_order_id)
            except BrokerError as exc:
                # Gone from Gateway open book — close local so recon stops MATERIAL_DRIFT.
                if "ibkr_order_not_found" in str(exc) or "order_not_found" in str(exc):
                    row.status = InternalOrderState.CANCELLED.value
                    row.raw_payload = {
                        **(row.raw_payload or {}),
                        "broker_sync": {"missing_remote": True, "error": str(exc)[:200]},
                    }
                    updated += 1
                    missing += 1
                    continue
                errors += 1
                logger.warning(
                    "order_status_sync_failed",
                    broker_order_id=row.broker_order_id,
                    error=str(exc),
                )
                continue
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.warning(
                    "order_status_sync_failed",
                    broker_order_id=row.broker_order_id,
                    error=str(exc),
                )
                continue
            new_status = _internal_status(br.status)
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
        logger.info(
            "orders_synced_from_broker",
            checked=len(rows),
            updated=updated,
            missing_remote=missing,
            errors=errors,
        )
        return {
            "checked": len(rows),
            "updated": updated,
            "missing_remote": missing,
            "errors": errors,
        }
