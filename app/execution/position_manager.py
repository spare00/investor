"""Position sync and portfolio snapshot from broker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import BrokerClient
from app.brokers.errors import BrokerError
from app.brokers.factory import get_broker
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models import PortfolioSnapshot, Position
from app.risk import PortfolioRiskView, PositionRiskView
from app.schemas.risk_manager import PortfolioStateInput, PositionSnapshot

logger = get_logger(__name__)

# Static sector map for concentration checks (extend later).
SECTOR_MAP: dict[str, str] = {
    "SPY": "Index",
    "QQQ": "Index",
    "IWM": "Index",
    "DIA": "Index",
    "EEM": "Index",
    "NVDA": "Technology",
    "MSFT": "Technology",
    "AMZN": "Consumer",
    "GOOGL": "Technology",
    "META": "Technology",
    "AVGO": "Technology",
    "AMD": "Technology",
    "AAPL": "Technology",
    "TSLA": "Consumer",
    "IONQ": "Technology",
    "PLTR": "Technology",
    "PATH": "Technology",
    "HPE": "Technology",
    "NOK": "Technology",
    "BB": "Technology",
    "NVTS": "Technology",
    "F": "Consumer",
    "SOFI": "Financials",
    "JBLU": "Consumer",
    "CORZ": "Technology",
    "IREN": "Technology",
    "WULF": "Technology",
    "SMR": "Utilities",
    "RDW": "Industrials",
    "QBTS": "Technology",
    "ONDS": "Technology",
    "KEEL": "Financials",
}


class PositionManager:
    def __init__(
        self,
        session: AsyncSession,
        *,
        broker: BrokerClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.broker = broker or get_broker(self.settings)

    async def sync_from_broker(
        self,
        *,
        account: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        force_snapshot: bool = False,
    ) -> dict[str, Any]:
        """Sync local positions from broker.

        Optional ``account`` / ``positions`` reuse a shared broker book (avoid
        triple-fetch during scheduled recon). Skips a new PortfolioSnapshot when
        equity/cash/position fingerprint is unchanged unless ``force_snapshot``.
        """
        try:
            if account is None or positions is None:
                account = await self.broker.get_account()
                positions = await self.broker.get_positions()
        except BrokerError:
            logger.exception("position_sync_failed")
            raise

        assert account is not None
        assert positions is not None

        now = datetime.now(UTC)
        equity = float(str(account.get("equity") or account.get("portfolio_value") or 0))
        cash = float(str(account.get("cash") or 0))
        cash_pct = (cash / equity * 100.0) if equity else 100.0

        # Upsert by symbol instead of wipe+rewrite.
        existing_rows = list((await self.session.execute(select(Position))).scalars().all())
        by_symbol = {p.symbol.upper(): p for p in existing_rows}
        seen: set[str] = set()
        gross = 0.0
        parsed: list[dict[str, Any]] = []
        for raw in positions:
            symbol = str(raw.get("symbol") or "").upper()
            qty = float(str(raw.get("qty") or 0))
            if not symbol or qty == 0:
                continue
            mv = float(str(raw.get("market_value") or 0))
            cost = float(str(raw.get("cost_basis") or 0))
            upnl = float(str(raw.get("unrealized_pl") or 0))
            avg = float(str(raw.get("avg_entry_price") or 0))
            gross += abs(mv)
            seen.add(symbol)
            parsed.append(
                {
                    "symbol": symbol,
                    "quantity": qty,
                    "avg_entry_price": avg,
                    "market_value": mv,
                    "cost_basis": cost,
                    "unrealized_pnl": upnl,
                }
            )
            row = by_symbol.get(symbol)
            if row is None:
                self.session.add(
                    Position(
                        id=uuid4(),
                        symbol=symbol,
                        quantity=qty,
                        avg_entry_price=avg,
                        market_value=mv,
                        cost_basis=cost,
                        unrealized_pnl=upnl,
                        sector=SECTOR_MAP.get(symbol, "Unknown"),
                        as_of=now,
                    )
                )
            else:
                row.quantity = qty
                row.avg_entry_price = avg
                row.market_value = mv
                row.cost_basis = cost
                row.unrealized_pnl = upnl
                row.sector = SECTOR_MAP.get(symbol, row.sector or "Unknown")
                row.as_of = now

        for symbol, row in by_symbol.items():
            if symbol not in seen:
                await self.session.delete(row)
        await self.session.flush()

        gross_pct = (gross / equity * 100.0) if equity else 0.0
        from app.brokers.models import redact_account_id

        safe_account = dict(account)
        if "id" in safe_account:
            safe_account["account_id_reference"] = redact_account_id(str(safe_account.pop("id")))
        for key in ("account_number", "account_id"):
            if key in safe_account:
                safe_account[key] = redact_account_id(str(safe_account[key]))

        fingerprint = {
            "equity": round(equity, 4),
            "cash": round(cash, 4),
            "positions": sorted(
                [
                    [p["symbol"], round(p["quantity"], 6), round(p["market_value"], 4)]
                    for p in parsed
                ]
            ),
        }
        snapshot_written = False
        snap: PortfolioSnapshot | None = None
        last = (
            await self.session.execute(
                select(PortfolioSnapshot).order_by(PortfolioSnapshot.as_of.desc()).limit(1)
            )
        ).scalar_one_or_none()
        last_fp = (last.payload or {}).get("fingerprint") if last is not None else None
        if force_snapshot or last_fp != fingerprint:
            snap = PortfolioSnapshot(
                id=uuid4(),
                as_of=now,
                equity=equity,
                cash=cash,
                cash_pct=cash_pct,
                gross_exposure_pct=gross_pct,
                daily_pnl=0.0,
                daily_pnl_pct=0.0,
                drawdown_pct=float(last.drawdown_pct) if last is not None else 0.0,
                peak_equity=max(equity, float(last.peak_equity) if last is not None else equity),
                open_positions=len(parsed),
                payload={
                    "account": safe_account,
                    "position_count": len(parsed),
                    "fingerprint": fingerprint,
                },
            )
            last_eq = account.get("last_equity")
            if last_eq is not None:
                prior = float(str(last_eq))
                snap.daily_pnl = equity - prior
                snap.daily_pnl_pct = ((equity - prior) / prior * 100.0) if prior else 0.0
            if snap.peak_equity > 0:
                snap.drawdown_pct = max(
                    0.0, (snap.peak_equity - equity) / snap.peak_equity * 100.0
                )
            self.session.add(snap)
            snapshot_written = True
            await self.session.flush()
        else:
            snap = last

        lifecycle_sync: dict[str, Any] = {}
        try:
            from app.intraday.monitor import PositionMonitor

            lifecycle_sync = await PositionMonitor(
                self.session, settings=self.settings
            ).sync_from_broker_positions(list(positions))
        except Exception as exc:  # noqa: BLE001
            logger.warning("lifecycle_sync_failed", error=str(exc)[:200])
            lifecycle_sync = {"error": str(exc)[:200]}

        from app.core.metrics import (
            OPEN_POSITIONS,
            PORTFOLIO_CASH,
            PORTFOLIO_DRAWDOWN_PCT,
            PORTFOLIO_EQUITY,
        )

        PORTFOLIO_EQUITY.set(equity)
        PORTFOLIO_CASH.set(cash)
        PORTFOLIO_DRAWDOWN_PCT.set(float(snap.drawdown_pct) if snap is not None else 0.0)
        OPEN_POSITIONS.set(len(parsed))

        logger.info(
            "positions_synced",
            equity=equity,
            cash=cash,
            positions=len(parsed),
            snapshot_written=snapshot_written,
            lifecycles=lifecycle_sync.get("upserted"),
            lifecycles_closed=lifecycle_sync.get("closed"),
        )
        return {
            "equity": equity,
            "cash": cash,
            "cash_pct": cash_pct,
            "gross_exposure_pct": gross_pct,
            "open_positions": len(parsed),
            "daily_pnl": float(snap.daily_pnl) if snap is not None else 0.0,
            "daily_pnl_pct": float(snap.daily_pnl_pct) if snap is not None else 0.0,
            "lifecycles": lifecycle_sync,
            "snapshot_written": snapshot_written,
        }

    async def portfolio_state_input(self) -> PortfolioStateInput:
        """Build PortfolioStateInput from latest DB snapshot / positions."""
        now = datetime.now(UTC)
        pos_result = await self.session.execute(select(Position))
        positions = list(pos_result.scalars().all())
        snap_result = await self.session.execute(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.as_of.desc()).limit(1)
        )
        snap = snap_result.scalar_one_or_none()
        if snap is None:
            return PortfolioStateInput(
                as_of=now,
                equity=self.settings.starting_cash,
                cash=self.settings.starting_cash,
                cash_pct=100.0,
                gross_exposure_pct=0.0,
            )
        equity = snap.equity or 1.0
        return PortfolioStateInput(
            as_of=snap.as_of,
            equity=snap.equity,
            cash=snap.cash,
            cash_pct=snap.cash_pct,
            gross_exposure_pct=snap.gross_exposure_pct,
            positions=[
                PositionSnapshot(
                    symbol=p.symbol,
                    quantity=p.quantity,
                    market_value=p.market_value,
                    cost_basis=p.cost_basis,
                    unrealized_pnl=p.unrealized_pnl,
                    sector=p.sector,
                    weight_pct=(p.market_value / equity * 100.0) if equity else 0.0,
                )
                for p in positions
            ],
            daily_pnl_pct=snap.daily_pnl_pct,
            drawdown_pct=snap.drawdown_pct,
        )

    async def load_for_risk(
        self,
        *,
        require_broker: bool | None = None,
    ) -> tuple[PortfolioStateInput, str]:
        """Load portfolio for risk/agents — prefer broker sync when connected.

        When paper orders or automation are armed, broker sync failure fails closed
        (raises) instead of silently sizing against ``starting_cash``.
        """
        if require_broker is None:
            require_broker = bool(
                self.settings.enable_broker_orders or self.settings.enable_automated_execution
            )
        want_sync = bool(
            self.settings.enable_broker_connection
            or self.settings.enable_broker_orders
            or require_broker
        )
        if want_sync:
            try:
                await self.sync_from_broker()
                return await self.portfolio_state_input(), "portfolio_from_broker"
            except Exception as exc:  # noqa: BLE001
                if require_broker:
                    logger.error("portfolio_sync_required_failed", error=str(exc)[:240])
                    raise
                logger.warning("portfolio_sync_fallback", error=str(exc)[:240])
                return await self.portfolio_state_input(), "portfolio_db_fallback"

        state = await self.portfolio_state_input()
        note = (
            "portfolio_from_db"
            if state.equity != self.settings.starting_cash or state.positions
            else "portfolio_starting_cash"
        )
        return state, note

    def to_risk_view(self, state: PortfolioStateInput) -> PortfolioRiskView:
        return PortfolioRiskView(
            equity=state.equity,
            cash=state.cash,
            cash_pct=state.cash_pct,
            gross_exposure_pct=state.gross_exposure_pct,
            positions=[
                PositionRiskView(
                    symbol=p.symbol,
                    quantity=p.quantity,
                    market_value=p.market_value,
                    sector=p.sector,
                    weight_pct=p.weight_pct,
                )
                for p in state.positions
            ],
            daily_pnl_pct=state.daily_pnl_pct,
            drawdown_pct=state.drawdown_pct,
            consecutive_losses=state.consecutive_losses,
            trading_halted=state.trading_halted,
            cooldown_until=state.cooldown_until,
            peak_equity=state.equity,
        )
