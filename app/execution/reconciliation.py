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
from app.models import BrokerReconciliationRun, Order


@dataclass(slots=True)
class BrokerBook:
    """One-shot broker snapshot shared by recon / poll / position sync."""

    orders: list[Any]
    positions: list[dict[str, Any]]
    account: dict[str, Any]


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

        local_open = list(
            (
                await self.session.execute(
                    select(Order).where(
                        Order.status.in_(
                            [
                                "new",
                                "accepted",
                                "partially_filled",
                                "SUBMITTED",
                                "ACCEPTED",
                                "SUBMITTING",
                                "PARTIALLY_FILLED",
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        local_ids = {o.broker_order_id for o in local_open if o.broker_order_id}
        remote_ids = {
            getattr(o, "broker_order_id", None)
            for o in remote_orders
            if getattr(o, "broker_order_id", None)
        }
        for oid in local_ids - remote_ids:
            issues.append({"type": "local_order_missing_remote", "broker_order_id": oid})
        for oid in remote_ids - local_ids:
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
            "book": book,
        }
