"""Stealth accumulation: multi-day same-price split-buy proxy."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents.cio import quant_entry_plans
from app.agents.quant_strategist import QuantStrategistAgent
from app.schemas.common import (
    LiquidityState,
    MarketRegime,
    MomentumState,
    SymbolAction,
    TrendState,
    VolatilityState,
)
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput, SessionBar
from app.universe.accumulation import (
    SessionPrint,
    collapse_snapshots_to_sessions,
    detect_stealth_accumulation,
    view_has_accumulation,
)
from app.universe.book_strategy import should_propose_entry, structure_allows_entry
from app.universe.entry_attribution import public_entry_reason


def _box(
    *, n: int = 5, close: float = 100.0, vol: float = 2_000_000.0, drift: float = 0.0
) -> list[SessionPrint]:
    out: list[SessionPrint] = []
    for i in range(n):
        px = close * (1.0 + drift * i / max(n - 1, 1))
        out.append(
            SessionPrint(
                session_date=f"2026-09-{10 + i:02d}",
                open=px * 0.999,
                high=px * 1.005,
                low=px * 0.995,
                close=px,
                volume=vol,
            )
        )
    return out


def test_insufficient_history_is_not_a_signal() -> None:
    hit = detect_stealth_accumulation(_box(n=3), avg_volume=2_000_000)
    assert hit.detected is False
    assert hit.reason == "insufficient_history"


def test_same_price_persistent_volume_is_detected() -> None:
    hit = detect_stealth_accumulation(_box(), avg_volume=2_000_000)
    assert hit.detected is True
    assert hit.reason == "stealth_split_buy"
    assert hit.sessions == 5
    assert hit.buy_volume_share is not None and hit.buy_volume_share >= 0.52


def test_wide_price_span_is_rejected() -> None:
    hit = detect_stealth_accumulation(_box(drift=0.04), avg_volume=2_000_000)
    assert hit.detected is False
    assert hit.reason == "close_span"


def test_sliding_box_is_rejected() -> None:
    hit = detect_stealth_accumulation(_box(drift=-0.02), avg_volume=2_000_000)
    assert hit.detected is False
    assert hit.reason == "sliding"


def test_distribution_volume_is_rejected() -> None:
    bars = _box()
    down = [
        SessionPrint(
            session_date=b.session_date,
            open=b.close * 1.004,
            high=b.high,
            low=b.low,
            close=b.close * 0.998,
            volume=b.volume,
        )
        for b in bars
    ]
    hit = detect_stealth_accumulation(down, avg_volume=2_000_000)
    assert hit.detected is False
    assert hit.reason == "distribution"


def test_volume_fade_is_rejected() -> None:
    bars = _box(vol=2_000_000)
    faded = [
        SessionPrint(
            session_date=b.session_date,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=200_000.0,
        )
        for b in bars
    ]
    hit = detect_stealth_accumulation(faded, avg_volume=2_000_000)
    assert hit.detected is False
    assert hit.reason == "volume_faded"


def test_collapse_keeps_latest_print_per_session() -> None:
    rows = [
        SimpleNamespace(
            symbol="MSFT",
            as_of=datetime(2026, 9, 16, 16, 0, tzinfo=UTC),
            last=99.0,
            open=99.0,
            high=99.5,
            low=98.5,
            volume=1_000_000,
        ),
        SimpleNamespace(
            symbol="MSFT",
            as_of=datetime(2026, 9, 16, 20, 0, tzinfo=UTC),
            last=100.0,
            open=99.2,
            high=100.4,
            low=98.8,
            volume=2_500_000,
        ),
        SimpleNamespace(
            symbol="MSFT",
            as_of=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
            last=100.2,
            open=100.0,
            high=100.6,
            low=99.7,
            volume=2_400_000,
        ),
    ]
    sessions = collapse_snapshots_to_sessions(rows, symbol="MSFT")
    assert [s.session_date for s in sessions] == ["2026-09-16", "2026-09-17"]
    assert sessions[0].close == 100.0
    assert sessions[0].volume == 2_500_000


def test_short_book_allows_accumulation_in_a_downtrend_base() -> None:
    ok, why = structure_allows_entry(
        horizon="short",
        trend=TrendState.DOWN,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=38.0,
        last=64.0,
        sma_20=66.0,
        accumulation=True,
    )
    assert ok is True
    assert why == "ok"
    assert should_propose_entry(
        horizon="short",
        probability=0.55,
        trend=TrendState.DOWN,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=38.0,
        regime=MarketRegime.RISK_ON,
        accumulation=True,
    )


def test_scalp_does_not_use_accumulation_to_bypass_sideways() -> None:
    ok, why = structure_allows_entry(
        horizon="scalp",
        trend=TrendState.SIDEWAYS,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=55.0,
        last=450.0,
        sma_20=450.0,
        accumulation=True,
    )
    assert ok is False
    assert why == "sideways_stand_down"


def test_quant_fallback_stamps_accumulation_on_short_box() -> None:
    history = [
        SessionBar(
            session_date=p.session_date,
            open=p.open,
            high=p.high,
            low=p.low,
            close=p.close,
            volume=p.volume,
        )
        for p in _box()
    ]
    payload = QuantStrategistInput(
        as_of=datetime(2026, 9, 18, 14, 0, tzinfo=UTC),
        symbol_bars=[
            BarSnapshot(
                symbol="BHP",
                last=100.0,
                open=99.9,
                high=100.5,
                low=99.5,
                volume=2_000_000,
                avg_volume_20d=2_000_000,
                atr_14=1.2,
                rsi_14=52.0,
                sma_20=100.0,
                sma_50=100.0,
                sma_200=95.0,
                session_history=history,
            )
        ],
        watchlist=[{"symbol": "BHP", "horizon": "short"}],
    )
    out = QuantStrategistAgent().fallback_output(payload, reason="local_python_owns")
    view = out.symbol_views[0]
    assert view.entry_timing == "accumulation"
    assert view_has_accumulation(view)
    assert any(n.startswith("stealth=") for n in view.notes)


def test_quant_entry_plans_follow_sideways_accumulation() -> None:
    from app.schemas.common import PriceZone
    from app.schemas.quant_strategist import SymbolQuantView

    view = SymbolQuantView(
        symbol="BHP",
        trend_state=TrendState.SIDEWAYS,
        momentum_state=MomentumState.STEADY,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        probability_estimate=0.62,
        probability_basis="stealth",
        entry_zone=PriceZone(min=99.2, max=100.8),
        stop_or_invalidation=97.0,
        entry_timing="accumulation",
        notes=["timing=accumulation", "stealth=stealth_split_buy"],
    )
    plans = quant_entry_plans(
        views=[view],
        watchlist=[{"symbol": "BHP", "horizon": "short"}],
        held_symbols=[],
        regime=MarketRegime.RISK_ON,
        max_position_pct=15.0,
        allowlist=["BHP"],
    )
    assert len(plans) == 1
    assert plans[0].symbol == "BHP"
    assert plans[0].action == SymbolAction.SCALE_IN


def test_accumulation_maps_to_stealth_buy_reason() -> None:
    assert public_entry_reason("accumulation") == "stealth_buy"
