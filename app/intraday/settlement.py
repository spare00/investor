"""Post-market settlement (idempotent by session_date + venue)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        venue: str | None = None,
    ) -> None:
        from app.market.venues import resolve_venue

        self.session = session
        self.settings = settings or get_settings()
        self.venue = resolve_venue(self.settings, venue=venue)
        self.bus = IntradayEventBus(session, settings=self.settings)

    async def settle(
        self, *, session_date: str | None = None, venue: str | None = None
    ) -> dict[str, Any]:
        from app.market.venues import resolve_venue

        book = resolve_venue(self.settings, venue=venue or self.venue.value).value
        day = session_date or datetime.now(UTC).date().isoformat()
        existing = await self._existing_settlement(day, book)

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

        day_start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        all_orders = list((await self.session.execute(select(Order))).scalars().all())
        orders = [o for o in all_orders if self._order_venue(o) == book]
        order_ids = {o.id for o in orders}
        executions = list((await self.session.execute(select(Execution))).scalars().all())
        executions = [e for e in executions if e.order_id in order_ids]
        # Prefer session-day executions when timestamps exist; else keep all (fixture/offline)
        day_execs = [
            e
            for e in executions
            if e.executed_at is None
            or (
                (e.executed_at if e.executed_at.tzinfo else e.executed_at.replace(tzinfo=UTC))
                >= day_start
                and (e.executed_at if e.executed_at.tzinfo else e.executed_at.replace(tzinfo=UTC))
                < day_end
            )
        ]
        if not day_execs and executions:
            # Offline tests often have no timezone-aligned dates — do not invent; mark limited
            scoped = executions
            scope_note = "all_executions_no_date_filter"
        else:
            scoped = day_execs
            scope_note = "session_day"

        by_sym: dict[str, list[Execution]] = {}
        for ex in sorted(scoped, key=lambda e: e.executed_at or datetime.now(UTC)):
            by_sym.setdefault(ex.symbol, []).append(ex)

        pnl_rows: list[dict[str, Any]] = []
        for symbol, fills in by_sym.items():
            lots: list[Lot] = []
            last: Any = None
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
                    select(PositionLifecycle).where(
                        PositionLifecycle.status.in_(
                            ["OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"]
                        ),
                        PositionLifecycle.venue == book,
                    )
                )
            )
            .scalars()
            .all()
        )
        payload = {
            "venue": book,
            "position_sync": sync,
            "recon": {k: v for k, v in recon.items() if k != "book"},
            "execution_scope": scope_note,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        account_json = account if isinstance(account, dict) else {}
        # Defensive: never persist non-JSON broker objects into JSON columns.
        try:
            import json as _json

            _json.dumps(account_json, default=str)
        except TypeError:
            account_json = {
                k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                for k, v in account_json.items()
            }
        if existing is None:
            settlement = PostmarketSettlement(
                id=uuid4(),
                session_date=day,
                reconciliation_result=recon.get("result"),
                account_snapshot=account_json,
                order_count=len(orders),
                execution_count=len(scoped),
                overnight_positions=[p.symbol for p in open_lc],
                pnl_summary=pnl_rows,
                payload=payload,
            )
            self.session.add(settlement)
        else:
            settlement = existing
            settlement.reconciliation_result = recon.get("result")
            settlement.account_snapshot = account_json
            settlement.order_count = len(orders)
            settlement.execution_count = len(scoped)
            settlement.overnight_positions = [p.symbol for p in open_lc]
            settlement.pnl_summary = pnl_rows
            settlement.payload = payload

        # Lot method only on TradePnL.method (VARCHAR 16). Session/venue live in
        # payload — ``FIFO:2026-08-20:AU`` is 18 chars and aborted Postgres flush,
        # which left postmarket_review stuck ``running`` until the stale reaper.
        lot_method = str(self.settings.position_lot_method or "FIFO")[:16]
        legacy_method = f"{lot_method}:{day}:{book}"
        for row in pnl_rows:
            tagged = await self._existing_trade_pnl(
                symbol=row["symbol"], day=day, book=book, legacy_method=legacy_method
            )
            if tagged is None:
                self.session.add(
                    TradePnL(
                        id=uuid4(),
                        symbol=row["symbol"],
                        gross_realized_pl=row["net_realized_pl"],
                        net_realized_pl=row["net_realized_pl"],
                        unrealized_pl=row["unrealized_pl"],
                        fees=0.0,
                        estimated_slippage=0.0,
                        return_pct=0.0,
                        method=lot_method,
                        conflict_with_broker=bool(row.get("conflict")),
                        payload={"session_date": day, "venue": book},
                    )
                )
            else:
                tagged.method = lot_method
                tagged.net_realized_pl = row["net_realized_pl"]
                tagged.gross_realized_pl = row["net_realized_pl"]
                tagged.unrealized_pl = row["unrealized_pl"]
                tagged.conflict_with_broker = bool(row.get("conflict"))
                payload = dict(tagged.payload) if isinstance(tagged.payload, dict) else {}
                payload["session_date"] = day
                payload["venue"] = book
                tagged.payload = payload

        await self.bus.publish(
            event_type="MARKET_CLOSED",
            source="settlement",
            deduplication_key=f"{book}:market_closed:{day}",
            requires_risk_review=False,
            importance="medium",
            payload={"venue": book},
        )
        await self.session.flush()
        return {
            "settlement_id": str(settlement.id),
            "session_date": day,
            "venue": book,
            "reconciliation": recon,
            "pnl": pnl_rows,
            "overnight_positions": settlement.overnight_positions,
            "broker_orders_submitted": False,
            "upserted": existing is not None,
        }

    async def _existing_trade_pnl(
        self, *, symbol: str, day: str, book: str, legacy_method: str
    ) -> TradePnL | None:
        rows = list(
            (
                await self.session.execute(select(TradePnL).where(TradePnL.symbol == symbol))
            )
            .scalars()
            .all()
        )
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if (
                str(payload.get("session_date") or "") == day
                and str(payload.get("venue") or "").upper() == book
            ):
                return row
            if str(row.method or "") == legacy_method:
                return row
        return None

    async def _existing_settlement(
        self, day: str, book: str
    ) -> PostmarketSettlement | None:
        rows = list(
            (
                await self.session.execute(
                    select(PostmarketSettlement).where(PostmarketSettlement.session_date == day)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            row_venue = str(payload.get("venue") or "US").upper()
            if row_venue == book:
                return row
        return None

    @staticmethod
    def _order_venue(order: Order) -> str:
        payload = order.raw_payload if isinstance(order.raw_payload, dict) else {}
        return str(payload.get("venue") or "US").upper()
