"""Freshness, quality scoring, and conflict detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.canonical.models import (
    CanonicalDataConflict,
    CanonicalQuote,
    ConflictState,
    DataQualityBreakdown,
    FreshnessState,
)
from app.core.config import Settings, get_settings
from app.market.calendar import MarketCalendarService


def freshness_state_for_quote(
    as_of: datetime,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    session_phase: str | None = None,
) -> FreshnessState:
    cfg = settings or get_settings()
    now = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    age = (now - as_of).total_seconds()
    # After hours: relax freshness (quotes don't update)
    if session_phase in {"POSTMARKET", "AFTER_HOURS", "NON_TRADING_DAY", "BEFORE_PREMARKET"}:
        if age <= 86400:
            return FreshnessState.ACCEPTABLE
        return FreshnessState.STALE
    max_age = cfg.latest_quote_max_age_seconds
    if age <= max_age:
        return FreshnessState.FRESH
    if age <= max_age * 4:
        return FreshnessState.ACCEPTABLE
    if age <= max_age * 20:
        return FreshnessState.STALE
    return FreshnessState.EXPIRED


def score_quality(
    *,
    freshness: FreshnessState,
    completeness: float,
    source_reliability: float,
    validation_ok: bool,
    agreement: float = 1.0,
    issues: list[str] | None = None,
) -> DataQualityBreakdown:
    fresh_map = {
        FreshnessState.FRESH: 1.0,
        FreshnessState.ACCEPTABLE: 0.8,
        FreshnessState.STALE: 0.4,
        FreshnessState.EXPIRED: 0.0,
        FreshnessState.UNKNOWN: 0.5,
    }
    f = fresh_map[freshness]
    v = 1.0 if validation_ok else 0.0
    overall = (
        0.3 * f
        + 0.25 * completeness
        + 0.2 * source_reliability
        + 0.15 * agreement
        + 0.1 * v
    )
    return DataQualityBreakdown(
        overall=round(overall, 4),
        freshness=f,
        completeness=completeness,
        source_reliability=source_reliability,
        cross_provider_agreement=agreement,
        validation=v,
        issues=issues or [],
    )


def validate_quote(quote: CanonicalQuote) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if quote.last < 0:
        issues.append("negative_price")
    if quote.bid is not None and quote.ask is not None and quote.ask < quote.bid:
        issues.append("ask_lt_bid")
    if quote.currency and quote.currency != "USD":
        issues.append("currency_mismatch")
    return (len(issues) == 0), issues


def validate_bar(open_: float, high: float, low: float, close: float, volume: float) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if any(x < 0 for x in (open_, high, low, close, volume)):
        issues.append("negative_ohlcv")
    if high < low:
        issues.append("high_lt_low")
    if close > high or close < low or open_ > high or open_ < low:
        issues.append("ohlc_out_of_range")
    return (len(issues) == 0), issues


def compare_quotes(
    primary: CanonicalQuote,
    secondary: CanonicalQuote,
    *,
    settings: Settings | None = None,
) -> CanonicalDataConflict:
    cfg = settings or get_settings()
    if primary.last <= 0:
        return CanonicalDataConflict(
            data_type="quote",
            symbol_or_key=primary.symbol,
            state=ConflictState.UNRESOLVED,
            provider_names=[primary.provenance.provider_name if primary.provenance else "?", secondary.provenance.provider_name if secondary.provenance else "?"],
        )
    diff_bps = abs(primary.last - secondary.last) / primary.last * 10_000.0
    tol = cfg.quote_price_tolerance_bps
    if diff_bps <= tol:
        state = ConflictState.AGREED if diff_bps < tol / 4 else ConflictState.MINOR_DIFFERENCE
    else:
        state = ConflictState.MATERIAL_CONFLICT
    return CanonicalDataConflict(
        data_type="quote",
        symbol_or_key=primary.symbol,
        state=state,
        primary_value=primary.last,
        secondary_value=secondary.last,
        difference=diff_bps,
        tolerance=tol,
        provider_names=[
            primary.provenance.provider_name if primary.provenance else "primary",
            secondary.provenance.provider_name if secondary.provenance else "secondary",
        ],
        details={"unit": "bps"},
    )


def surprise(actual: float | None, consensus: float | None) -> tuple[float | None, str | None]:
    if actual is None or consensus is None:
        return None, None
    value = actual - consensus
    direction = "higher" if value > 0 else "lower" if value < 0 else "inline"
    return value, direction


def session_phase_now(settings: Settings | None = None, now: datetime | None = None) -> str:
    return MarketCalendarService(settings or get_settings()).get_market_status(now).phase
