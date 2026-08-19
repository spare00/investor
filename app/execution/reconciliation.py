"""Broker ↔ local reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.factory import get_broker
from app.brokers.models import ReconciliationResult
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models import BrokerReconciliationRun, Order

logger = get_logger(__name__)

_OPEN_LOCAL_STATUSES = [
    "new",
    "accepted",
    "partially_filled",
    "pending_submit",
    "SUBMITTED",
    "ACCEPTED",
    "SUBMITTING",
    "PARTIALLY_FILLED",
]

_REMOTE_STATUS_TO_LOCAL = {
    "new": "ACCEPTED",
    "accepted": "ACCEPTED",
    "pending_new": "SUBMITTED",
    "partially_filled": "PARTIALLY_FILLED",
    "filled": "FILLED",
    "canceled": "CANCELLED",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
    "expired": "EXPIRED",
}


@dataclass(slots=True)
class BrokerBook:
    """One-shot broker snapshot shared by recon / poll / position sync."""

    orders: list[Any]
    positions: list[dict[str, Any]]
    account: dict[str, Any]


def _remote_status_value(remote: Any) -> str:
    status = getattr(remote, "status", None)
    if status is None:
        return "accepted"
    return str(getattr(status, "value", status) or "accepted").lower()


def _fields_from_remote(remote: Any) -> dict[str, Any]:
    raw = dict(getattr(remote, "raw", None) or {})
    symbol = str(raw.get("symbol") or "UNKNOWN").upper()[:32]
    side = str(raw.get("side") or "buy").lower()
    if side not in {"buy", "sell"}:
        side = "buy"
    try:
        qty = float(raw.get("qty") or getattr(remote, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    order_type = str(raw.get("order_type") or raw.get("type") or "market")[:32]
    local_status = _REMOTE_STATUS_TO_LOCAL.get(_remote_status_value(remote), "ACCEPTED")
    submitted_at = getattr(remote, "submitted_at", None)
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "order_type": order_type,
        "status": local_status,
        "submitted_at": submitted_at,
        "raw": raw,
    }


class ReconciliationService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.broker = get_broker(self.settings)

    async def fetch_book(self) -> BrokerBook:
        """Single round-trip set: open orders + positions + account."""
        remote_orders = await self.broker.get_open_orders()
        remote_positions = await self.broker.get_positions()
        account = await self.broker.get_account()
        return BrokerBook(
            orders=list(remote_orders or []),
            positions=[dict(p) for p in (remote_positions or [])],
            account=dict(account or {}),
        )

    async def _known_broker_order_ids(self, remote_ids: set[str]) -> set[str]:
        if not remote_ids:
            return set()
        rows = (
            await self.session.execute(
                select(Order.broker_order_id).where(Order.broker_order_id.in_(list(remote_ids)))
            )
        ).scalars().all()
        return {str(oid) for oid in rows if oid}

    async def _adopt_remote_orders(self, remotes: list[Any]) -> int:
        adopted = 0
        for remote in remotes:
            oid = str(getattr(remote, "broker_order_id", "") or "")
            if not oid:
                continue
            fields = _fields_from_remote(remote)
            row = Order(
                id=uuid4(),
                broker_order_id=oid,
                idempotency_key=f"adopted:{oid}",
                symbol=fields["symbol"],
                side=fields["side"],
                qty=fields["qty"],
                order_type=fields["order_type"],
                status=fields["status"],
                submitted_at=fields["submitted_at"],
                raw_payload={"adopted": True, "broker": fields["raw"]},
            )
            self.session.add(row)
            adopted += 1
        if adopted:
            await self.session.flush()
            logger.warning("recon_adopted_remote_orders", count=adopted)
        return adopted

    async def run(
        self,
        sync_type: str = "ON_DEMAND",
        *,
        book: BrokerBook | None = None,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        if book is None:
            try:
                book = await self.fetch_book()
            except Exception as exc:  # noqa: BLE001
                run = BrokerReconciliationRun(
                    id=uuid4(),
                    sync_type=sync_type,
                    result=ReconciliationResult.BROKER_UNAVAILABLE.value,
                    issues=[{"error": str(exc)[:300]}],
                    payload={},
                )
                self.session.add(run)
                await self.session.flush()
                return {
                    "result": run.result,
                    "issues": run.issues,
                    "id": str(run.id),
                    "book": None,
                }

        remote_orders = book.orders
        remote_positions = book.positions
        account = book.account

        remote_ids = {
            str(getattr(o, "broker_order_id", None))
            for o in remote_orders
            if getattr(o, "broker_order_id", None)
        }
        known_ids = await self._known_broker_order_ids(remote_ids)
        to_adopt = [
            o
            for o in remote_orders
            if str(getattr(o, "broker_order_id", "") or "") not in known_ids
            and getattr(o, "broker_order_id", None)
        ]
        adopted = await self._adopt_remote_orders(to_adopt) if to_adopt else 0

        local_open = list(
            (
                await self.session.execute(
                    select(Order).where(Order.status.in_(_OPEN_LOCAL_STATUSES))
                )
            )
            .scalars()
            .all()
        )
        local_open_ids = {o.broker_order_id for o in local_open if o.broker_order_id}
        known_ids = await self._known_broker_order_ids(remote_ids)

        if remote_orders:
            for oid in local_open_ids - remote_ids:
                issues.append({"type": "local_order_missing_remote", "broker_order_id": oid})
        elif local_open_ids:
            # Incomplete IBKR open-order snapshot (reconnect / clientId scope)
            # must not freeze the book as MATERIAL_DRIFT.
            issues.append(
                {
                    "type": "empty_remote_open_orders",
                    "local_open": len(local_open_ids),
                }
            )
        for oid in remote_ids - known_ids:
            issues.append({"type": "remote_order_missing_local", "broker_order_id": oid})

        result = ReconciliationResult.IN_SYNC
        if issues:
            material = any(
                i["type"].startswith("remote_") or i["type"].startswith("local_") for i in issues
            )
            result = ReconciliationResult.MATERIAL_DRIFT if material else ReconciliationResult.MINOR_DRIFT

        run = BrokerReconciliationRun(
            id=uuid4(),
            sync_type=sync_type,
            result=result.value,
            issues=issues,
            payload={
                "account_cash": account.get("cash"),
                "remote_open_orders": len(remote_orders),
                "remote_positions": len(remote_positions),
                "adopted_orders": adopted,
                "as_of": datetime.now(UTC).isoformat(),
            },
        )
        self.session.add(run)
        await self.session.flush()
        return {
            "result": result.value,
            "issues": issues,
            "id": str(run.id),
            "blocks_new_orders": result == ReconciliationResult.MATERIAL_DRIFT,
            "adopted_orders": adopted,
            "book": book,
        }
