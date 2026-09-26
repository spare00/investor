"""Liquidity screens are written on the US tape; AU has to be scaled onto them.

The AU allowlist is BHP, CBA, VAS, IOZ, NDQ, JPEQ — two ASX-20 stocks and four
ETFs. Against unscaled US share-count bars (5M shares for scalp, 2M for day)
only BHP cleared the intraday books: CBA trades ~2.5M shares because it costs
~A$170, and an ETF's on-screen volume understates its liquidity because market
makers create and redeem against the basket instead of trading the listed line.

Two changes, tested here: the horizon bar is turnover rather than share count,
so price level stops deciding liquidity; and both the universe screen and the
risk engine's hard veto scale their US numbers onto the venue.
"""

from __future__ import annotations

from app.market.venues import (
    VENUE_SPECS,
    Venue,
    venue_liquidity_floor,
    venue_spread_cap,
)
from app.risk.engine import DeterministicRiskEngine
from app.risk.types import (
    PortfolioRiskView,
    RiskLimits,
    TradeIntent,
    VetoCode,
)
from app.universe.horizons import UniverseHorizon, policy_for


def test_us_is_unscaled() -> None:
    assert venue_liquidity_floor("US", 1_000_000) == 1_000_000
    assert venue_spread_cap("US", 20.0) == 20.0


def test_au_gets_a_lower_floor_and_a_wider_spread() -> None:
    assert venue_liquidity_floor("AU", 1_000_000) < 1_000_000
    assert venue_spread_cap("AU", 20.0) > 20.0


def test_an_unknown_venue_is_screened_as_strictly_as_the_us() -> None:
    """A bad venue string must not wave a name through, or crash collection."""
    for bad in (None, "", "XX", "nasdaq"):
        assert venue_liquidity_floor(bad, 1_000_000) == 1_000_000
        assert venue_spread_cap(bad, 20.0) == 20.0


def test_us_carries_the_strictest_multipliers() -> None:
    """Which is what makes the unknown-venue fallback conservative."""
    us = VENUE_SPECS[Venue.US]
    for spec in VENUE_SPECS.values():
        # A higher floor is stricter; a lower spread cap is stricter.
        assert spec.liquidity_floor_mult <= us.liquidity_floor_mult
        assert spec.spread_cap_mult >= us.spread_cap_mult


def test_the_horizon_bars_still_tighten_toward_the_short_end() -> None:
    order = [
        UniverseHorizon.MEDIUM,
        UniverseHorizon.SHORT,
        UniverseHorizon.DAY,
        UniverseHorizon.SCALP,
    ]
    floors = [policy_for(h).min_daily_turnover for h in order]
    caps = [policy_for(h).max_spread_bps for h in order]

    assert floors == sorted(floors)
    assert caps == sorted(caps, reverse=True)


def _trade(**overrides: object) -> TradeIntent:
    defaults: dict[str, object] = {
        "symbol": "VAS",
        "side": "buy",
        "quantity": 10.0,
        "entry_price": 100.0,
        "stop_loss": 97.0,
        "avg_daily_volume": 400_000.0,
        "bid_ask_spread_bps": 30.0,
        "expected_slippage_bps": 5.0,
        "venue": "AU",
    }
    defaults.update(overrides)
    return TradeIntent(**defaults)  # type: ignore[arg-type]


def _failed_codes(trade: TradeIntent) -> set[VetoCode | str]:
    engine = DeterministicRiskEngine(
        limits=RiskLimits(min_avg_daily_volume=1_000_000.0, max_bid_ask_spread_bps=20.0)
    )
    portfolio = PortfolioRiskView(
        equity=100_000.0,
        cash=90_000.0,
        cash_pct=90.0,
        gross_exposure_pct=10.0,
        positions=[],
        daily_pnl_pct=0.0,
        drawdown_pct=0.0,
        consecutive_losses=0,
    )
    result = engine.evaluate_pretrade(
        portfolio,
        trade,
        allowlist={trade.symbol},
        data_quality_score=0.9,
        market_session_clear=True,
        broker_data_consistent=True,
    )
    return {c.code for c in result.checks if not c.passed}


def test_an_au_etf_is_no_longer_vetoed_by_a_us_share_count() -> None:
    """VAS at ~400k shares/day was vetoed by a 1M floor set for the US tape."""
    codes = _failed_codes(_trade())

    assert VetoCode.INSUFFICIENT_VOLUME not in codes
    assert VetoCode.EXCESSIVE_SPREAD not in codes


def test_a_genuinely_thin_au_name_is_still_vetoed() -> None:
    codes = _failed_codes(_trade(avg_daily_volume=1_000.0, bid_ask_spread_bps=300.0))

    assert VetoCode.INSUFFICIENT_VOLUME in codes
    assert VetoCode.EXCESSIVE_SPREAD in codes


def test_the_us_veto_is_unchanged() -> None:
    """Same numbers that pass on AU must still fail on the US tape."""
    codes = _failed_codes(_trade(symbol="SPY", venue="US"))

    assert VetoCode.INSUFFICIENT_VOLUME in codes
    assert VetoCode.EXCESSIVE_SPREAD in codes
