"""CIO / Final Decision Maker I/O schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.schemas.common import (
    MarketRegime,
    OrderType,
    PortfolioAction,
    PriceZone,
    StrictModel,
    SymbolAction,
    TimeHorizon,
    TraceMetadata,
)
from app.schemas.devils_advocate import DevilsAdvocateOutput
from app.schemas.macro_strategist import MacroStrategistOutput
from app.schemas.market_intelligence import MarketIntelligenceOutput
from app.schemas.quant_strategist import QuantStrategistOutput
from app.schemas.risk_manager import PositionSnapshot, RiskManagerOutput


class SymbolActionPlan(StrictModel):
    symbol: str
    action: SymbolAction
    confidence: int = Field(ge=0, le=100)
    target_position_pct: float = Field(ge=0.0, le=100.0)
    order_type: OrderType = OrderType.LIMIT
    entry_zone: PriceZone | None = None
    stop_loss: float | None = None
    take_profit: list[float] = Field(default_factory=list)
    time_horizon: TimeHorizon = TimeHorizon.INTRADAY
    thesis: str
    invalidation: str
    max_holding_time_minutes: int | None = None

    @model_validator(mode="after")
    def _require_exit_for_entries(self) -> SymbolActionPlan:
        entry_actions = {
            SymbolAction.STRONG_BUY,
            SymbolAction.BUY,
            SymbolAction.SCALE_IN,
        }
        if self.action in entry_actions:
            if self.stop_loss is None and not self.invalidation.strip():
                raise ValueError(
                    f"{self.symbol}: new entries require stop_loss or invalidation"
                )
        return self


class CIOInput(StrictModel):
    as_of: datetime
    market_intelligence: MarketIntelligenceOutput
    macro: MacroStrategistOutput
    quant: QuantStrategistOutput
    risk: RiskManagerOutput
    devil: DevilsAdvocateOutput
    portfolio_cash_pct: float
    positions: list[PositionSnapshot] = Field(default_factory=list)
    allowlist: list[str] = Field(default_factory=list)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class CIODecision(StrictModel):
    decision_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime
    market_regime: MarketRegime
    portfolio_action: PortfolioAction
    symbol_actions: list[SymbolActionPlan] = Field(default_factory=list)
    cash_target_pct: float = Field(ge=0.0, le=100.0)
    hedge_required: bool = False
    risk_approval: bool
    risk_conditions: list[str] = Field(default_factory=list)
    reason_not_to_trade: str | None = None
    hard_veto_honored: bool = True
    trace: TraceMetadata = Field(default_factory=TraceMetadata)

    @model_validator(mode="after")
    def _honor_risk_veto(self) -> CIODecision:
        if not self.risk_approval:
            blocked = {
                PortfolioAction.STRONG_BUY,
                PortfolioAction.BUY,
                PortfolioAction.SCALE_IN,
                PortfolioAction.HEDGE,
            }
            if self.portfolio_action in blocked:
                raise ValueError(
                    "CIO cannot approve risk-increasing actions when risk_approval is false"
                )
            for plan in self.symbol_actions:
                if plan.action in {
                    SymbolAction.STRONG_BUY,
                    SymbolAction.BUY,
                    SymbolAction.SCALE_IN,
                }:
                    raise ValueError(
                        f"CIO cannot emit {plan.action} for {plan.symbol} without risk approval"
                    )
        return self
