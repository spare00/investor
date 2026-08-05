"""Trade universe eligibility checks (allowlist + liquidity + quality)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.services.normalize import NormalizedMarketSnapshot

# Common leveraged ETF prefixes/symbols excluded by default.
_LEVERAGED_DEFAULT = {
    "TQQQ",
    "SQQQ",
    "UPRO",
    "SPXU",
    "SOXL",
    "SOXS",
    "TECL",
    "TECS",
}


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    symbol: str
    eligible: bool
    reasons: tuple[str, ...]


def is_leveraged_etf(symbol: str) -> bool:
    return symbol.upper() in _LEVERAGED_DEFAULT


def evaluate_symbol_eligibility(
    snapshot: NormalizedMarketSnapshot,
    *,
    settings: Settings | None = None,
    halted: bool = False,
    entry_universe: set[str] | None = None,
) -> EligibilityResult:
    cfg = settings or get_settings()
    symbol = snapshot.symbol.upper()
    reasons: list[str] = []

    allowed = entry_universe if entry_universe is not None else cfg.allowlist_set()
    if symbol not in allowed:
        reasons.append("not_in_allowlist")
    if halted:
        reasons.append("trading_halted")
    if snapshot.last < cfg.penny_stock_max_price:
        reasons.append("penny_stock")
    if is_leveraged_etf(symbol) and not cfg.allow_leveraged_etfs:
        reasons.append("leveraged_etf")
    if snapshot.avg_volume_20d is not None and snapshot.avg_volume_20d < cfg.min_avg_daily_volume:
        reasons.append("insufficient_volume")
    if snapshot.spread_bps is not None and snapshot.spread_bps > cfg.max_bid_ask_spread_bps:
        reasons.append("excessive_spread")
    if snapshot.quality_score < cfg.min_data_quality_score:
        reasons.append("low_data_quality")
    if snapshot.last <= 0:
        reasons.append("invalid_price")

    return EligibilityResult(symbol=symbol, eligible=not reasons, reasons=tuple(reasons))
