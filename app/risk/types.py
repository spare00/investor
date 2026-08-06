"""Risk Engine types and interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class VetoCode(StrEnum):
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_DRAWDOWN = "max_drawdown"
    MAX_POSITION_PCT = "max_position_pct"
    MAX_SECTOR_PCT = "max_sector_pct"
    MIN_CASH_PCT = "min_cash_pct"
    MAX_GROSS_EXPOSURE = "max_gross_exposure"
    MAX_OPEN_POSITIONS = "max_open_positions"
    CONSECUTIVE_LOSSES_COOLDOWN = "consecutive_losses_cooldown"
    CONSECUTIVE_LOSSES_HALT = "consecutive_losses_halt"
    MISSING_STOP = "missing_stop_or_invalidation"
    EXCESSIVE_SLIPPAGE = "excessive_slippage"
    INSUFFICIENT_VOLUME = "insufficient_volume"
    EXCESSIVE_SPREAD = "excessive_spread"
    LOW_DATA_QUALITY = "low_data_quality"
    NON_LIVE_MARKET_PRICES = "non_live_market_prices"
    BROKER_DATA_MISMATCH = "broker_data_mismatch"
    MARKET_SESSION_UNCLEAR = "market_session_unclear"
    NOT_IN_ALLOWLIST = "not_in_allowlist"
    PENNY_STOCK = "penny_stock"
    LEVERAGED_ETF = "leveraged_etf"
    TRADING_HALTED = "trading_halted"
    RISK_PER_TRADE = "risk_per_trade_exceeded"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    starting_cash: float = 25_000.0
    max_position_pct: float = 10.0
    max_sector_pct: float = 30.0
    max_gross_exposure_pct: float = 70.0
    min_cash_pct: float = 30.0
    risk_per_trade_pct: float = 0.5
    daily_max_loss_pct: float = 1.5
    max_drawdown_pct: float = 8.0
    max_open_positions: int = 8
    max_consecutive_losses: int = 3
    cooldown_after_loss_minutes: int = 30
    max_consecutive_losses_halt_day: int = 5
    min_avg_daily_volume: float = 1_000_000.0
    max_bid_ask_spread_bps: float = 20.0
    min_data_quality_score: float = 0.6
    max_slippage_bps: float = 15.0
    penny_stock_max_price: float = 5.0
    allow_leveraged_etfs: bool = False


@dataclass(slots=True)
class PositionRiskView:
    symbol: str
    quantity: float
    market_value: float
    sector: str
    weight_pct: float


@dataclass(slots=True)
class PortfolioRiskView:
    equity: float
    cash: float
    cash_pct: float
    gross_exposure_pct: float
    positions: list[PositionRiskView] = field(default_factory=list)
    daily_pnl_pct: float = 0.0
    drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    trading_halted: bool = False
    cooldown_until: datetime | None = None
    peak_equity: float | None = None


@dataclass(slots=True)
class TradeIntent:
    symbol: str
    side: str  # buy | sell
    quantity: float
    entry_price: float
    stop_loss: float | None = None
    invalidation: str | None = None
    expected_slippage_bps: float | None = None
    avg_daily_volume: float | None = None
    bid_ask_spread_bps: float | None = None
    atr: float | None = None
    sector: str = "Unknown"
    is_leveraged_etf: bool = False
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    code: VetoCode | str
    passed: bool
    message: str
    hard: bool = True
    details: dict[str, float | str | bool | None] = field(default_factory=dict)


@dataclass(slots=True)
class SizingResult:
    shares: int
    dollar_risk: float
    stop_distance: float
    position_notional: float
    capped_by: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PreTradeRiskResult:
    approved: bool
    halt_day: bool
    checks: list[CheckResult]
    hard_vetoes: list[str]
    sizing: SizingResult | None = None
    adjusted_quantity: float | None = None

    @property
    def veto_codes(self) -> list[str]:
        return [c.code if isinstance(c.code, str) else c.code.value for c in self.checks if not c.passed and c.hard]


class RiskEngine(Protocol):
    """Deterministic risk calculations — no LLM involvement."""

    def position_size(
        self,
        *,
        equity: float,
        entry_price: float,
        stop_price: float,
        atr: float | None = None,
        existing_position_value: float = 0.0,
    ) -> SizingResult: ...

    def stop_distance(self, entry_price: float, stop_price: float) -> float: ...

    def drawdown_pct(self, peak_equity: float, equity: float) -> float: ...

    def sector_exposure_pct(
        self,
        positions: list[PositionRiskView],
        equity: float,
        sector: str,
        additional_notional: float = 0.0,
    ) -> float: ...

    def evaluate_pretrade(
        self,
        portfolio: PortfolioRiskView,
        trade: TradeIntent,
        *,
        allowlist: set[str],
        data_quality_score: float,
        market_session_clear: bool,
        broker_data_consistent: bool,
        now: datetime | None = None,
        seen_idempotency_keys: set[str] | None = None,
    ) -> PreTradeRiskResult: ...
