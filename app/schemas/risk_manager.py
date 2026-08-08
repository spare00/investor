"""Portfolio & Risk Manager I/O schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import RiskVerdict, StrictModel, TraceMetadata
from app.schemas.macro_strategist import MacroStrategistOutput
from app.schemas.market_intelligence import MarketIntelligenceOutput
from app.schemas.quant_strategist import QuantStrategistOutput


class PositionSnapshot(StrictModel):
    symbol: str
    quantity: float
    market_value: float
    cost_basis: float
    unrealized_pnl: float
    sector: str
    weight_pct: float


class ProposedTrade(StrictModel):
    symbol: str
    side: str  # buy | sell
    quantity: float | None = None
    notional: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    invalidation: str | None = None
    expected_slippage_bps: float | None = None
    avg_daily_volume: float | None = None
    bid_ask_spread_bps: float | None = None
    atr: float | None = None
    sector: str | None = None
    idempotency_key: str | None = None


class PortfolioStateInput(StrictModel):
    as_of: datetime
    equity: float
    cash: float
    cash_pct: float
    gross_exposure_pct: float
    positions: list[PositionSnapshot] = Field(default_factory=list)
    daily_pnl_pct: float = 0.0
    drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    open_order_count: int = 0
    trading_halted: bool = False
    cooldown_until: datetime | None = None


class RiskManagerInput(StrictModel):
    as_of: datetime
    portfolio: PortfolioStateInput
    proposed_trades: list[ProposedTrade] = Field(default_factory=list)
    market_intelligence: MarketIntelligenceOutput | None = None
    macro: MacroStrategistOutput | None = None
    quant: QuantStrategistOutput | None = None
    data_quality_score: float = Field(default=1.0, ge=0.0, le=1.0)
    market_session_clear: bool = True
    broker_data_consistent: bool = True
    # Risk Officer owns present-market price integrity for any order path.
    live_prices_required: bool = False
    price_feed_live: bool = True
    price_providers: list[str] = Field(default_factory=list)
    price_integrity_notes: list[str] = Field(default_factory=list)
    watchlist: list[dict] = Field(
        default_factory=list,
        description="Active watchlist rows with horizon/risk multipliers",
    )
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class TradeRiskAdjustment(StrictModel):
    symbol: str
    original_quantity: float | None = None
    approved_quantity: float | None = None
    verdict: RiskVerdict
    reasons: list[str] = Field(default_factory=list)


class RiskManagerOutput(StrictModel):
    timestamp: datetime
    overall_verdict: RiskVerdict
    hard_vetoes: list[str] = Field(default_factory=list)
    soft_warnings: list[str] = Field(default_factory=list)
    trade_adjustments: list[TradeRiskAdjustment] = Field(default_factory=list)
    halt_new_trades: bool = False
    cash_pct: float
    gross_exposure_pct: float
    notes: list[str] = Field(default_factory=list)
    # Deterministic engine payload (not LLM narrative)
    engine_checks: list[dict[str, object]] = Field(default_factory=list)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)
