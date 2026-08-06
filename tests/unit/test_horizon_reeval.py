"""Horizon re-eval cadence tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.intraday.events import IntradayEventBus
from app.universe.reeval import (
    global_reeval_gap_minutes,
    min_reeval_seconds_for_symbols,
    planned_intraday_interval_minutes,
    reeval_seconds_for_horizon,
    symbol_reeval_gap_minutes,
)


def test_scalp_reeval_faster_than_medium() -> None:
    settings = Settings(intraday_min_reeval_seconds=300)
    assert reeval_seconds_for_horizon("scalp", settings) == 120
    assert reeval_seconds_for_horizon("medium", settings) == 3600
    assert reeval_seconds_for_horizon(None, settings) == 300


def test_planned_interval_follows_tightest_passed_horizon() -> None:
    settings = Settings(intraday_reevaluation_interval_minutes=20, max_intraday_reanalyses=24)
    assert planned_intraday_interval_minutes([], settings) == 20
    assert planned_intraday_interval_minutes(None, settings) == 20
    assert planned_intraday_interval_minutes(["medium"], settings) == 60
    assert planned_intraday_interval_minutes(["short"], settings) == 15
    assert planned_intraday_interval_minutes(["day"], settings) == 5
    # Callers must pass open/focus horizons — not full watchlist — so this
    # only densifies when a scalp book is actually under review.
    assert planned_intraday_interval_minutes(["scalp", "medium"], settings) == 2
    assert planned_intraday_interval_minutes(["medium"], settings) == 60


def test_align_cio_horizons_from_watchlist() -> None:
    from uuid import uuid4

    from app.schemas.cio import CIODecision, SymbolActionPlan
    from app.schemas.common import (
        MarketRegime,
        PortfolioAction,
        SymbolAction,
        TimeHorizon,
        TraceMetadata,
    )
    from app.universe.horizons import align_cio_horizons

    decision = CIODecision(
        decision_id=uuid4(),
        timestamp=datetime.now(UTC),
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.SCALE_IN,
        symbol_actions=[
            SymbolActionPlan(
                symbol="MSFT",
                action=SymbolAction.SCALE_IN,
                confidence=60,
                target_position_pct=5.0,
                thesis="theme",
                invalidation="break",
                time_horizon=TimeHorizon.INTRADAY,
            )
        ],
        cash_target_pct=90.0,
        risk_approval=True,
        trace=TraceMetadata(),
    )
    out = align_cio_horizons(
        decision, [{"symbol": "MSFT", "horizon": "medium"}]
    )
    assert out.symbol_actions[0].time_horizon is TimeHorizon.POSITION
    assert out.symbol_actions[0].max_holding_time_minutes == 60 * 24 * 60


def test_planned_interval_floors_by_llm_budget() -> None:
    settings = Settings(intraday_reevaluation_interval_minutes=30, max_intraday_reanalyses=12)
    # 360m session / ceil(12*1.5)=18 jobs → 20m floor → scalp 2m becomes 20m
    assert (
        planned_intraday_interval_minutes(["scalp"], settings, session_minutes=360) == 20
    )
    # Medium still 60 (above floor)
    assert (
        planned_intraday_interval_minutes(["medium"], settings, session_minutes=360) == 60
    )


def test_min_among_open_books_picks_tightest() -> None:
    settings = Settings(intraday_min_reeval_seconds=300)
    secs = min_reeval_seconds_for_symbols(
        ["MSFT", "QQQ"],
        {"MSFT": "medium", "QQQ": "scalp"},
        settings,
    )
    assert secs == 120


def test_event_bus_respects_horizon_symbol_gap() -> None:
    settings = Settings(
        max_intraday_reanalyses=20,
        max_symbol_reanalyses_per_day=20,
        min_global_reanalysis_gap_minutes=10,
        min_symbol_reanalysis_gap_minutes=10,
    )
    bus = IntradayEventBus.__new__(IntradayEventBus)
    bus.settings = settings
    bus._reanalysis_times = []
    bus._symbol_reanalysis = {}

    horizons = {"QQQ": "scalp", "MSFT": "medium"}
    now = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)
    ok, _ = bus.allow_reanalysis(symbols=["QQQ"], now=now, horizon_by_symbol=horizons)
    assert ok
    bus.record_reanalysis(["QQQ"], now=now)

    # 90s later — scalp needs 120s → still cooling (global checked first)
    later = now + timedelta(seconds=90)
    ok2, why = bus.allow_reanalysis(symbols=["QQQ"], now=later, horizon_by_symbol=horizons)
    assert ok2 is False
    assert why == "global_cooldown"

    # After 2+ minutes scalp ok
    later2 = now + timedelta(seconds=130)
    ok3, _ = bus.allow_reanalysis(symbols=["QQQ"], now=later2, horizon_by_symbol=horizons)
    assert ok3

    # Medium-only book needs ~60 minutes
    bus._reanalysis_times = [now]
    bus._symbol_reanalysis = {"MSFT": [now]}
    mid = now + timedelta(minutes=15)
    ok4, why4 = bus.allow_reanalysis(symbols=["MSFT"], now=mid, horizon_by_symbol=horizons)
    assert ok4 is False
    assert global_reeval_gap_minutes(["MSFT"], horizons, settings) == 60.0
    assert symbol_reeval_gap_minutes("MSFT", horizons, settings) == 60.0
    assert why4 == "global_cooldown"
