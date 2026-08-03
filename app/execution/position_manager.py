"""Position sync and portfolio snapshot from broker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.alpaca import BrokerError, get_broker
from app.brokers.base import BrokerClient
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

    async def sync_from_broker(self) -> dict[str, Any]:
        try:
            account = await self.broker.get_account()
            positions = await self.broker.get_positions()
        except BrokerError as exc:
            logger.exception("position_sync_failed")
            raise

        now = datetime.now(UTC)
        equity = float(str(account.get("equity") or account.get("portfolio_value") or 0))
        cash = float(str(account.get("cash") or 0))
        cash_pct = (cash / equity * 100.0) if equity else 100.0

        # Replace local position rows with broker truth
        existing = await self.session.execute(select(Position))
        for row in existing.scalars().all():
            await self.session.delete(row)
        await self.session.flush()

        gross = 0.0
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

        gross_pct = (gross / equity * 100.0) if equity else 0.0
        snap = PortfolioSnapshot(
            id=uuid4(),
            as_of=now,
            equity=equity,
            cash=cash,
            cash_pct=cash_pct,
            gross_exposure_pct=gross_pct,
            daily_pnl=float(str(account.get("last_equity") or equity)) and 0.0,
            daily_pnl_pct=0.0,
            drawdown_pct=0.0,
            peak_equity=equity,
            open_positions=len(positions),
            payload={"account": account},
        )
        # Rough daily pnl if last_equity present
        last_eq = account.get("last_equity")
        if last_eq is not None:
            last = float(str(last_eq))
            snap.daily_pnl = equity - last
            snap.daily_pnl_pct = ((equity - last) / last * 100.0) if last else 0.0

        self.session.add(snap)
        await self.session.flush()

        from app.core.metrics import (
            OPEN_POSITIONS,
            PORTFOLIO_CASH,
            PORTFOLIO_DRAWDOWN_PCT,
            PORTFOLIO_EQUITY,
        )

        PORTFOLIO_EQUITY.set(equity)
        PORTFOLIO_CASH.set(cash)
        PORTFOLIO_DRAWDOWN_PCT.set(snap.drawdown_pct)
        OPEN_POSITIONS.set(len(positions))

        logger.info(
            "positions_synced",
            equity=equity,
            cash=cash,
            positions=len(positions),
        )
        return {
            "equity": equity,
            "cash": cash,
            "cash_pct": cash_pct,
            "gross_exposure_pct": gross_pct,
            "open_positions": len(positions),
            "daily_pnl": snap.daily_pnl,
            "daily_pnl_pct": snap.daily_pnl_pct,
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
