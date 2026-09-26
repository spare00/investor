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
    horizon: str | None = None,
    venue: str | None = None,
) -> EligibilityResult:
    from app.market.venues import venue_for_symbol, venue_liquidity_floor, venue_spread_cap

    cfg = settings or get_settings()
    symbol = snapshot.symbol.upper()
    reasons: list[str] = []

    # The settings floor is a share count; a horizon's is turnover. Keep them
    # apart rather than comparing one against the other's number.
    min_vol = cfg.min_avg_daily_volume
    min_turnover: float | None = None
    max_spread = cfg.max_bid_ask_spread_bps
    if horizon:
        try:
            from app.universe.horizons import policy_for

            pol = policy_for(horizon)
            min_vol = None
            min_turnover = float(pol.min_daily_turnover)
            max_spread = float(pol.max_spread_bps)
        except ValueError:
            pass

    # Every liquidity number in the book is calibrated on the US tape, so scale
    # it onto the venue the symbol actually trades on before comparing.
    where = venue or getattr(snapshot, "venue", None) or venue_for_symbol(symbol, cfg).value
    if min_vol is not None:
        min_vol = venue_liquidity_floor(where, min_vol)
    if min_turnover is not None:
        min_turnover = venue_liquidity_floor(where, min_turnover)
    max_spread = venue_spread_cap(where, max_spread)

    allowed = entry_universe if entry_universe is not None else cfg.allowlist_set()
    if symbol not in allowed:
        reasons.append("not_in_allowlist")
    if halted:
        reasons.append("trading_halted")
    if snapshot.last < cfg.penny_stock_max_price:
        reasons.append("penny_stock")
    if is_leveraged_etf(symbol) and not cfg.allow_leveraged_etfs:
        reasons.append("leveraged_etf")
    if snapshot.avg_volume_20d is not None:
        if min_turnover is not None:
            if float(snapshot.avg_volume_20d) * max(0.0, float(snapshot.last)) < min_turnover:
                reasons.append("insufficient_turnover")
        elif min_vol is not None and snapshot.avg_volume_20d < min_vol:
            reasons.append("insufficient_volume")
    if snapshot.spread_bps is not None and snapshot.spread_bps > max_spread:
        reasons.append("excessive_spread")
    if snapshot.quality_score < cfg.min_data_quality_score:
        reasons.append("low_data_quality")
    if snapshot.last <= 0:
        reasons.append("invalid_price")

    return EligibilityResult(symbol=symbol, eligible=not reasons, reasons=tuple(reasons))
