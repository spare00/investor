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
from app.intraday.fills import fill_from_execution
from app.intraday.pnl import as_utc, reconstruct_fifo
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

        orders_by_id = {order.id: order for order in orders}
        fills = []
        for ex in executions:
            when = ex.executed_at
            if when is not None and as_utc(when) >= day_end:
                continue
            fill = fill_from_execution(ex, orders_by_id.get(ex.order_id))
            if fill is not None:
                fills.append(fill)
        ledger = reconstruct_fifo(fills)
        day_symbols = {
            fill.symbol for fill in fills if day_start <= as_utc(fill.executed_at) < day_end
        }
        pnl_rows: list[dict[str, Any]] = []
        for fifo_book in ledger.books:
            day_closes = [
                close
                for close in fifo_book.closes
                if close.closed_at is not None and day_start <= as_utc(close.closed_at) < day_end
            ]
            if fifo_book.symbol not in day_symbols and not day_closes:
                continue
            known = [close for close in day_closes if close.gross_pnl is not None]
            unknown = [close for close in day_closes if close.gross_pnl is None]
            gross = (
                round(sum(float(close.gross_pnl or 0.0) for close in known), 4) if known else None
            )
            fee_parts = [close.fee for close in known]
            fee = (
                round(sum(float(part) for part in fee_parts), 4)
                if known and all(part is not None for part in fee_parts)
                else None
            )
            net = None if gross is None else gross if fee is None else round(gross - fee, 4)
            pnl_rows.append(
                {
                    "symbol": fifo_book.symbol,
                    "currency": fifo_book.currency,
                    "gross_realized_pl": gross,
                    "net_realized_pl": net,
                    "fees": fee,
                    "fees_known": fee is not None and gross is not None,
                    "unknown_basis_count": len(unknown),
                    "pnl_unavailable": None
                    if gross is not None or not unknown
                    else "unknown_opening_inventory",
                    "conflict": False,
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
            "execution_scope": "fifo_history_through_day_end",
            "session_fill_scope": scope_note,
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
            gross = row.get("gross_realized_pl")
            net = row.get("net_realized_pl")
            stored_gross = float(gross) if gross is not None else 0.0
            stored_net = float(net) if net is not None else 0.0
            stored_fees = float(row["fees"]) if row.get("fees") is not None else 0.0
            row_payload = {
                "session_date": day,
                "venue": book,
                "currency": row.get("currency"),
                "fees_known": bool(row.get("fees_known")),
                "unknown_basis_count": int(row.get("unknown_basis_count") or 0),
                "pnl_unavailable": row.get("pnl_unavailable"),
            }
            if tagged is None:
                self.session.add(
                    TradePnL(
                        id=uuid4(),
                        symbol=row["symbol"],
                        gross_realized_pl=stored_gross,
                        net_realized_pl=stored_net,
                        unrealized_pl=0.0,
                        fees=stored_fees,
                        estimated_slippage=0.0,
                        return_pct=0.0,
                        method=lot_method,
                        conflict_with_broker=bool(row.get("conflict")),
                        payload=row_payload,
                    )
                )
            else:
                tagged.method = lot_method
                tagged.gross_realized_pl = stored_gross
                tagged.net_realized_pl = stored_net
                tagged.unrealized_pl = 0.0
                tagged.fees = stored_fees
                tagged.conflict_with_broker = bool(row.get("conflict"))
                payload = dict(tagged.payload) if isinstance(tagged.payload, dict) else {}
                payload.update(row_payload)
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
            (await self.session.execute(select(TradePnL).where(TradePnL.symbol == symbol)))
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

    async def _existing_settlement(self, day: str, book: str) -> PostmarketSettlement | None:
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
