"""Broker order update normalizer — polling fallback (streaming optional)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.factory import get_broker
from app.brokers.models import InternalOrderState, assert_order_transition
from app.core.config import Settings, get_settings
from app.intraday.events import IntradayEventBus
from app.models import BrokerOrderEvent, Order


_STATUS_MAP = {
    "new": InternalOrderState.ACCEPTED,
    "accepted": InternalOrderState.ACCEPTED,
    "pending_new": InternalOrderState.SUBMITTED,
    "partially_filled": InternalOrderState.PARTIALLY_FILLED,
    "filled": InternalOrderState.FILLED,
    "canceled": InternalOrderState.CANCELLED,
    "cancelled": InternalOrderState.CANCELLED,
    "pending_cancel": InternalOrderState.CANCEL_PENDING,
    "pending_replace": InternalOrderState.REPLACE_PENDING,
    "replaced": InternalOrderState.REPLACED,
    "rejected": InternalOrderState.REJECTED,
    "expired": InternalOrderState.EXPIRED,
    "unknown": InternalOrderState.UNKNOWN,
}

_OPENISH = {
    "new",
    "accepted",
    "partially_filled",
    "pending_submit",
    "pending_new",
    "SUBMITTED",
    "ACCEPTED",
    "SUBMITTING",
    "PARTIALLY_FILLED",
    "CANCEL_PENDING",
    "REPLACE_PENDING",
}


class BrokerUpdateProcessor:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.broker = get_broker(self.settings)
        self.bus = IntradayEventBus(session, settings=self.settings)

    async def poll_and_apply(self, *, remote_orders: list[Any] | None = None) -> dict[str, Any]:
        """Polling fallback when streaming disabled.

        Pass ``remote_orders`` from a shared broker book to avoid a second
        ``get_open_orders`` call during scheduled reconciliation.
        """
        updated = 0
        skipped_stale = 0
        skipped_unchanged = 0
        try:
            if remote_orders is None:
                remote_orders = await self.broker.get_open_orders()
            # Only open-ish local rows — not the full order history.
            local = list(
                (
                    await self.session.execute(select(Order).where(Order.status.in_(list(_OPENISH))))
                )
                .scalars()
                .all()
            )
        except Exception as exc:  # noqa: BLE001
            await self.bus.publish(
                event_type="DATA_STALE",
                source="broker_poll",
                deduplication_key=f"broker_poll_fail:{datetime.now(UTC).strftime('%Y%m%d%H%M')}",
                requires_risk_review=True,
                importance="high",
                payload={"error": str(exc)[:200]},
                bypass_cooldown=True,
            )
            return {"updated": 0, "error": str(exc)[:200], "fallback": "reconciliation_required"}

        remote_by_id = {
            getattr(o, "broker_order_id", None): o
            for o in (remote_orders or [])
            if getattr(o, "broker_order_id", None)
        }
        for row in local:
            if not row.broker_order_id:
                continue
            remote = remote_by_id.get(row.broker_order_id)
            # Local still open but missing from open-orders → likely filled/cancelled;
            # fetch once by id. Skip when remote list was empty due to broker outage
            # (handled above).
            if remote is None and hasattr(self.broker, "get_order"):
                try:
                    remote = await self.broker.get_order(row.broker_order_id)
                except Exception:  # noqa: BLE001
                    continue
            if remote is None:
                continue
            applied = await self.apply_remote_status(row, remote.status.value, remote)
            if applied == "updated":
                updated += 1
            elif applied == "stale":
                skipped_stale += 1
            elif applied == "unchanged":
                skipped_unchanged += 1
        return {
            "updated": updated,
            "skipped_stale": skipped_stale,
            "skipped_unchanged": skipped_unchanged,
            "mode": "polling",
        }

    async def apply_remote_status(self, row: Order, raw_status: str, remote: Any) -> str:
        mapped = _STATUS_MAP.get(raw_status.lower(), InternalOrderState.UNKNOWN)
        # No-op when already in sync — avoid BrokerOrderEvent / bus write amp.
        if row.status == mapped.value:
            return "unchanged"

        event_ts = getattr(remote, "submitted_at", None) or datetime.now(UTC)
        self.session.add(
            BrokerOrderEvent(
                id=uuid4(),
                order_id=row.id,
                broker_order_id=row.broker_order_id,
                event_type=raw_status.lower(),
                broker_status=raw_status,
                event_at=event_ts if getattr(event_ts, "tzinfo", None) else datetime.now(UTC),
                payload={
                    "filled_qty": getattr(remote, "filled_qty", None),
                    "avg_fill_price": getattr(remote, "avg_fill_price", None),
                },
            )
        )
        terminal = {
            InternalOrderState.FILLED.value,
            InternalOrderState.CANCELLED.value,
            InternalOrderState.REJECTED.value,
            InternalOrderState.EXPIRED.value,
            InternalOrderState.REPLACED.value,
        }
        if row.status in terminal and mapped.value not in terminal:
            await self.session.flush()
            return "stale"
        try:
            if row.status in {s.value for s in InternalOrderState}:
                assert_order_transition(InternalOrderState(row.status), mapped)
        except ValueError:
            row.raw_payload = {**(row.raw_payload or {}), "illegal_transition_observed": True}
            row.status = InternalOrderState.RECONCILIATION_REQUIRED.value
            await self.session.flush()
            return "reconciliation"
        row.status = mapped.value
        await self.session.flush()
        etype = "ORDER_FILLED" if mapped == InternalOrderState.FILLED else "BROKER_ORDER_UPDATE"
        if mapped == InternalOrderState.PARTIALLY_FILLED:
            etype = "ORDER_PARTIALLY_FILLED"
        elif mapped == InternalOrderState.CANCELLED:
            etype = "ORDER_CANCELLED"
        elif mapped == InternalOrderState.REJECTED:
            etype = "ORDER_REJECTED"
        await self.bus.publish(
            event_type=etype,
            source="broker_update",
            symbols=[row.symbol],
            deduplication_key=f"order:{row.broker_order_id}:{mapped.value}",
            order_id=row.id,
            requires_execution_review=mapped
            in {InternalOrderState.REJECTED, InternalOrderState.UNKNOWN},
            payload={"status": mapped.value},
        )
        return "updated"
