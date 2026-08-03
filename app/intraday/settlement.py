"""Post-market settlement."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.factory import get_broker
from app.core.config import Settings, get_settings
from app.execution.position_manager import PositionManager
from app.execution.reconciliation import ReconciliationService
from app.intraday.events import IntradayEventBus
from app.intraday.pnl import Lot, apply_fill_fifo
from app.models import Execution, Order, PositionLifecycle, PostmarketSettlement, TradePnL


class SettlementService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.bus = IntradayEventBus(session, settings=self.settings)

    async def settle(self) -> dict[str, Any]:
        recon = await ReconciliationService(self.session, settings=self.settings).run("POSTMARKET")
        try:
            sync = await PositionManager(self.session, settings=self.settings).sync_from_broker()
        except Exception as exc:  # noqa: BLE001
            sync = {"error": str(exc)[:200]}

        broker = get_broker(self.settings)
        try:
            account = await broker.get_account()
            if hasattr(broker, "get_account_canonical"):
                account = (await broker.get_account_canonical()).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            account = {"error": str(exc)[:200]}

        orders = list((await self.session.execute(select(Order))).scalars().all())
        executions = list((await self.session.execute(select(Execution))).scalars().all())

        # FIFO realized from executions grouped by symbol
        by_sym: dict[str, list[Execution]] = {}
        for ex in sorted(executions, key=lambda e: e.executed_at or datetime.now(UTC)):
            by_sym.setdefault(ex.symbol, []).append(ex)

        pnl_rows: list[dict[str, Any]] = []
        for symbol, fills in by_sym.items():
            lots: list[Lot] = []
            last: Any = None
            # Infer side from linked order when possible
            for fill in fills:
                order = await self.session.get(Order, fill.order_id)
                side = (order.side if order else "buy").lower()
                last = apply_fill_fifo(
                    lots,
                    side=side,
                    quantity=float(fill.qty),
                    price=float(fill.price),
                    fee=0.0,
                    equity=float(self.settings.starting_cash),
                )
                lots = last.remaining_lots
            if last is not None:
                row = TradePnL(
                    id=uuid4(),
                    symbol=symbol,
                    gross_realized_pl=last.gross_realized_pl,
                    net_realized_pl=last.net_realized_pl,
                    unrealized_pl=last.unrealized_pl,
                    fees=last.fees,
                    estimated_slippage=last.estimated_slippage,
                    return_pct=last.return_pct,
                    method=self.settings.position_lot_method,
                    conflict_with_broker=last.conflict_with_broker,
                    payload={},
                )
                self.session.add(row)
                pnl_rows.append(
                    {
                        "symbol": symbol,
                        "net_realized_pl": last.net_realized_pl,
                        "unrealized_pl": last.unrealized_pl,
                        "conflict": last.conflict_with_broker,
                    }
                )

        open_lc = list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING"]))
                )
            )
            .scalars()
            .all()
        )
        settlement = PostmarketSettlement(
            id=uuid4(),
            session_date=datetime.now(UTC).date().isoformat(),
            reconciliation_result=recon.get("result"),
            account_snapshot=account if isinstance(account, dict) else {},
            order_count=len(orders),
            execution_count=len(executions),
            overnight_positions=[p.symbol for p in open_lc],
            pnl_summary=pnl_rows,
            payload={"position_sync": sync, "recon": recon},
        )
        self.session.add(settlement)
        await self.bus.publish(
            event_type="MARKET_CLOSED",
            source="settlement",
            deduplication_key=f"market_closed:{settlement.session_date}",
            requires_risk_review=False,
            importance="medium",
        )
        await self.session.flush()
        return {
            "settlement_id": str(settlement.id),
            "reconciliation": recon,
            "pnl": pnl_rows,
            "overnight_positions": settlement.overnight_positions,
            "broker_orders_submitted": False,
        }
