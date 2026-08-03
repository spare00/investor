"""Devil's Advocate I/O schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.schemas.common import StrictModel, TraceMetadata
from app.schemas.macro_strategist import MacroStrategistOutput
from app.schemas.market_intelligence import MarketIntelligenceOutput
from app.schemas.quant_strategist import QuantStrategistOutput
from app.schemas.risk_manager import RiskManagerOutput


class DevilRecommendation(StrEnum):
    PROCEED = "PROCEED"
    PROCEED_WITH_CAUTION = "PROCEED_WITH_CAUTION"
    REDUCE_SIZE = "REDUCE_SIZE"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"


class ProposedThesis(StrictModel):
    symbol: str | None = None
    direction: str  # long | short | flat
    summary: str
    supporting_points: list[str] = Field(default_factory=list)


class DevilsAdvocateInput(StrictModel):
    as_of: datetime
    proposed_theses: list[ProposedThesis] = Field(default_factory=list)
    market_intelligence: MarketIntelligenceOutput | None = None
    macro: MacroStrategistOutput | None = None
    quant: QuantStrategistOutput | None = None
    risk: RiskManagerOutput | None = None
    consensus_lean: str | None = None
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class DevilsAdvocateOutput(StrictModel):
    timestamp: datetime
    strongest_reason_thesis_is_wrong: str
    information_already_in_price: bool
    information_already_in_price_rationale: str
    opposing_market_scenario: str
    prefer_no_trade: bool
    prefer_no_trade_rationale: str
    immediate_withdrawal_conditions: list[str] = Field(default_factory=list)
    confirmation_bias_flags: list[str] = Field(default_factory=list)
    crowd_trade_risk: bool = False
    trap_risk: str | None = None  # bull_trap | bear_trap | none
    alternative_strategies: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    data_conflicts: list[str] = Field(default_factory=list)
    challenge_score: float = Field(ge=0.0, le=1.0)
    recommendation: DevilRecommendation | None = None
    challenge_severity: float | None = Field(default=None, ge=0.0, le=1.0)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)
