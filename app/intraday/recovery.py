"""Intraday recovery after restart."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.execution.ops_persistence import restore_trading_controls
from app.execution.reconciliation import ReconciliationService
from app.execution.safety_controls import trading_controls
from app.intraday.broker_updates import BrokerUpdateProcessor
from app.intraday.events import IntradayEventBus
from app.models import IntradayEvent, IntradayRecoveryRun, Order, PositionLifecycle
from sqlalchemy import select


class IntradayRecoveryService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.bus = IntradayEventBus(session, settings=self.settings)

    async def run(self) -> dict[str, Any]:
        actions: list[str] = []
        # 1. Emergency state
        restored = await restore_trading_controls(self.session, trading_controls)
        if restored and restored.get("state") == "emergency_stop":
            actions.append("emergency_stop_restored")
        emergency = trading_controls.snapshot().state.value == "emergency_stop"

        # 2. Broker reconciliation before allowing new orders
        recon = await ReconciliationService(self.session, settings=self.settings).run("RECOVERY")
        actions.append(f"reconciliation:{recon.get('result')}")
        new_orders_allowed = not emergency and recon.get("result") not in {
            "MATERIAL_DRIFT",
            "BROKER_UNAVAILABLE",
            "LOCAL_STATE_INVALID",
        }

        # 3. Order status poll
        poll = await BrokerUpdateProcessor(self.session, settings=self.settings).poll_and_apply()
        actions.append(f"broker_poll_updated:{poll.get('updated')}")

        # 4. Position lifecycles present
        open_pos = list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(
                        PositionLifecycle.status.in_(["OPEN", "PENDING_OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"])
                    )
                )
            )
            .scalars()
            .all()
        )
        actions.append(f"open_lifecycles:{len(open_pos)}")

        # 5. Pending approvals / unknown orders
        unknown = list(
            (
                await self.session.execute(
                    select(Order).where(Order.status.in_(["UNKNOWN", "RECONCILIATION_REQUIRED", "pending_submit"]))
                )
            )
            .scalars()
            .all()
        )
        actions.append(f"unknown_orders:{len(unknown)}")

        # 6. Unprocessed events
        pending_events = list(
            (
                await self.session.execute(
                    select(IntradayEvent).where(IntradayEvent.status.in_(["NEW", "QUEUED"]))
                )
            )
            .scalars()
            .all()
        )
        actions.append(f"pending_events:{len(pending_events)}")

        run = IntradayRecoveryRun(
            id=uuid4(),
            emergency_stop=emergency,
            new_orders_allowed=new_orders_allowed,
            actions=actions,
            payload={"recon": recon, "poll": poll, "restored_controls": restored},
        )
        self.session.add(run)
        await self.session.flush()
        return {
            "recovery_id": str(run.id),
            "emergency_stop": emergency,
            "new_orders_allowed": new_orders_allowed,
            "actions": actions,
            "as_of": datetime.now(UTC).isoformat(),
        }
