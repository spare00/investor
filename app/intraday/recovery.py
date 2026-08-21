"""Intraday recovery after restart."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.ops import emit_emergency_stop_alert, emit_reconciliation_alert
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.ops_persistence import restore_trading_controls
from app.execution.position_manager import PositionManager
from app.execution.reconciliation import ReconciliationService
from app.execution.safety_controls import trading_controls
from app.intraday.broker_updates import BrokerUpdateProcessor
from app.intraday.events import IntradayEventBus
from app.models import IntradayEvent, IntradayRecoveryRun, Order, PositionLifecycle
from sqlalchemy import select

logger = get_logger(__name__)


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
        if emergency:
            await emit_emergency_stop_alert(
                self.session,
                self.settings,
                reason=str(trading_controls.snapshot().reason or "restored"),
                source="intraday_recovery",
            )

        # 2. Broker reconciliation before allowing new orders (one shared book).
        recon_svc = ReconciliationService(self.session, settings=self.settings)
        book = None
        try:
            book = await recon_svc.fetch_book()
            recon = await recon_svc.run("RECOVERY", book=book)
        except Exception:  # noqa: BLE001
            recon = await recon_svc.run("RECOVERY")
            book = recon.get("book")
        actions.append(f"reconciliation:{recon.get('result')}")
        await emit_reconciliation_alert(
            self.session,
            self.settings,
            result=str(recon.get("result") or ""),
            issues=list(recon.get("issues") or []),
            sync_type="RECOVERY",
        )
        new_orders_allowed = not emergency and recon.get("result") not in {
            "MATERIAL_DRIFT",
            "BROKER_UNAVAILABLE",
            "LOCAL_STATE_INVALID",
        }

        # 3. Order status poll + position sync (reuse book when available)
        if book is not None:
            poll = await BrokerUpdateProcessor(self.session, settings=self.settings).poll_and_apply(
                remote_orders=book.orders
            )
        else:
            poll = await BrokerUpdateProcessor(self.session, settings=self.settings).poll_and_apply()
        actions.append(f"broker_poll_updated:{poll.get('updated')}")

        # 3b. Mirror broker positions into PositionLifecycle
        lifecycle_sync: dict[str, Any] = {}
        try:
            if book is not None:
                sync = await PositionManager(self.session, settings=self.settings).sync_from_broker(
                    account=book.account,
                    positions=book.positions,
                )
            else:
                sync = await PositionManager(self.session, settings=self.settings).sync_from_broker()
            lifecycle_sync = sync.get("lifecycles") or {}
            actions.append(
                f"lifecycles_upserted:{lifecycle_sync.get('upserted', 0)}"
                f"/closed:{lifecycle_sync.get('closed', 0)}"
            )
        except Exception as exc:  # noqa: BLE001
            lifecycle_sync = {"error": str(exc)[:200]}
            actions.append("lifecycles_sync_error")
            logger.warning("intraday_recovery_lifecycle_sync_failed", error=str(exc)[:200])

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

        try:
            from app.intraday.session_hygiene import fold_session_residue

            fold = await fold_session_residue(self.session)
            if any(fold.values()):
                actions.append(
                    "session_fold:"
                    + ",".join(f"{k}={v}" for k, v in fold.items() if v)
                )
        except Exception as exc:  # noqa: BLE001
            actions.append(f"session_fold_error:{str(exc)[:80]}")

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

        recon_payload = {k: v for k, v in recon.items() if k != "book"}
        run = IntradayRecoveryRun(
            id=uuid4(),
            emergency_stop=emergency,
            new_orders_allowed=new_orders_allowed,
            actions=actions,
            payload={
                "recon": recon_payload,
                "poll": poll,
                "lifecycle_sync": lifecycle_sync,
                "restored_controls": restored,
            },
        )
        self.session.add(run)
        await self.session.flush()
        return {
            "recovery_id": str(run.id),
            "emergency_stop": emergency,
            "new_orders_allowed": new_orders_allowed,
            "actions": actions,
            "lifecycle_sync": lifecycle_sync,
            "as_of": datetime.now(UTC).isoformat(),
        }
