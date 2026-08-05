"""Shared enums and common schema building blocks."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MarketRegime(StrEnum):
    STRONG_RISK_ON = "STRONG_RISK_ON"
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"
    STRONG_RISK_OFF = "STRONG_RISK_OFF"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class PortfolioAction(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    SCALE_IN = "SCALE_IN"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    PARTIAL_SELL = "PARTIAL_SELL"
    SELL = "SELL"
    HEDGE = "HEDGE"
    STAY_CASH = "STAY_CASH"
    NO_TRADE = "NO_TRADE"


class SymbolAction(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    SCALE_IN = "SCALE_IN"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    PARTIAL_SELL = "PARTIAL_SELL"
    SELL = "SELL"
    HEDGE = "HEDGE"
    STAY_CASH = "STAY_CASH"
    NO_TRADE = "NO_TRADE"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class NewsCategory(StrEnum):
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    ANALYST = "analyst"
    FED = "fed"
    MACRO = "macro"
    GEOPOLITICS = "geopolitics"
    CORPORATE = "corporate"
    REGULATORY = "regulatory"
    OTHER = "other"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeHorizon(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"
    POSITION = "position"


class RiskVerdict(StrEnum):
    APPROVED = "approved"
    CONDITIONAL = "conditional"
    SIZE_REDUCED = "size_reduced"
    REJECTED = "rejected"
    HALT_DAY = "halt_day"


class TrendState(StrEnum):
    STRONG_UP = "strong_up"
    UP = "up"
    SIDEWAYS = "sideways"
    DOWN = "down"
    STRONG_DOWN = "strong_down"


class MomentumState(StrEnum):
    ACCELERATING = "accelerating"
    STEADY = "steady"
    DECELERATING = "decelerating"
    EXHAUSTED = "exhausted"


class VolatilityState(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    ELEVATED = "elevated"
    EXTREME = "extreme"


class BreadthState(StrEnum):
    STRONG = "strong"
    HEALTHY = "healthy"
    MIXED = "mixed"
    WEAK = "weak"
    DETERIORATING = "deteriorating"


class LiquidityState(StrEnum):
    AMPLE = "ample"
    NORMAL = "normal"
    TIGHT = "tight"
    STRESSED = "stressed"


class AgentName(StrEnum):
    MARKET_INTELLIGENCE = "market_intelligence"
    MACRO_STRATEGIST = "macro_strategist"
    QUANT_STRATEGIST = "quant_strategist"
    RISK_MANAGER = "risk_manager"
    DEVILS_ADVOCATE = "devils_advocate"
    CIO = "cio"
    UNIVERSE_MANAGER = "universe_manager"


class TraceMetadata(StrictModel):
    """Audit fields attached to every agent run / decision."""

    source_data_timestamp: datetime | None = None
    source_names: list[str] = Field(default_factory=list)
    agent_version: str = "0.1.0"
    prompt_version: str = "0.1.0"
    prompt_sha256: str | None = None
    schema_version: str = "1.0.0"
    model_name: str | None = None
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = None
    decision_timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    run_id: UUID = Field(default_factory=uuid4)


class PriceZone(StrictModel):
    min: float
    max: float

    def contains(self, price: float) -> bool:
        return self.min <= price <= self.max


class Scenario(StrictModel):
    name: str
    description: str
    probability: float = Field(ge=0.0, le=1.0)
    target_price: float | None = None
