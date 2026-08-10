"""Unit tests for DeterministicRiskEngine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.risk import (
    DeterministicRiskEngine,
    PortfolioRiskView,
    PositionRiskView,
    RiskLimits,
    TradeIntent,
    VetoCode,
)


@pytest.fixture
def engine() -> DeterministicRiskEngine:
    return DeterministicRiskEngine(
        RiskLimits(
            max_position_pct=10.0,
            max_sector_pct=30.0,
            max_gross_exposure_pct=70.0,
            max_venue_gross_pct=50.0,
            min_cash_pct=30.0,
            risk_per_trade_pct=0.5,
            daily_max_loss_pct=1.5,
            max_drawdown_pct=8.0,
            max_open_positions=8,
            max_consecutive_losses=3,
            max_consecutive_losses_halt_day=5,
            min_avg_daily_volume=1_000_000,
            max_bid_ask_spread_bps=20,
            min_data_quality_score=0.6,
            max_slippage_bps=15,
            penny_stock_max_price=5.0,
        )
    )


@pytest.fixture
def portfolio() -> PortfolioRiskView:
    return PortfolioRiskView(
        equity=25_000.0,
        cash=15_000.0,
        cash_pct=60.0,
        gross_exposure_pct=40.0,
        positions=[
            PositionRiskView(
                symbol="SPY",
                quantity=10,
                market_value=5_000.0,
                sector="Index",
                weight_pct=20.0,
            )
        ],
        daily_pnl_pct=0.0,
        drawdown_pct=1.0,
        consecutive_losses=0,
    )


def _buy(
    symbol: str = "QQQ",
    qty: float = 10,
    price: float = 480.0,
    stop: float | None = 474.0,
    **kwargs: object,
) -> TradeIntent:
    return TradeIntent(
        symbol=symbol,
        side="buy",
        quantity=qty,
        entry_price=price,
        stop_loss=stop,
        sector=str(kwargs.pop("sector", "Index")),
        avg_daily_volume=float(kwargs.pop("avg_daily_volume", 20_000_000)),
        bid_ask_spread_bps=float(kwargs.pop("bid_ask_spread_bps", 5)),
        expected_slippage_bps=float(kwargs.pop("expected_slippage_bps", 5)),
        **kwargs,  # type: ignore[arg-type]
    )


class TestPositionSizing:
    def test_sizes_by_risk_budget(self, engine: DeterministicRiskEngine) -> None:
        # equity 25k * 0.5% = $125 risk; stop distance $2 → 62 shares raw
        # max position 10% = $2500 at $40 → 62 shares fit without position cap
        result = engine.position_size(
            equity=25_000, entry_price=40.0, stop_price=38.0
        )
        assert result.dollar_risk == 125.0
        assert result.stop_distance == 2.0
        assert result.shares == 62
        assert result.capped_by == []

    def test_caps_by_max_position_pct(self, engine: DeterministicRiskEngine) -> None:
        # Max position 10% of 25k = 2500; at $100 → max 25 shares
        # Risk would allow 125/2 = 62 shares → capped
        result = engine.position_size(
            equity=25_000, entry_price=100.0, stop_price=98.0
        )
        assert result.shares == 25
        assert "max_position_pct" in result.capped_by

    def test_high_price_capped_before_risk_shares(
        self, engine: DeterministicRiskEngine
    ) -> None:
        # $480 * 20 would be risk-ok but exceeds 10% notional → 5 shares
        result = engine.position_size(
            equity=25_000, entry_price=480.0, stop_price=474.0
        )
        assert result.shares == 5
        assert "max_position_pct" in result.capped_by

    def test_atr_widens_stop_distance(self, engine: DeterministicRiskEngine) -> None:
        # Use cheap shares so position-pct does not dominate ATR sizing
        tight = engine.position_size(
            equity=25_000, entry_price=20.0, stop_price=19.0, atr=3.0
        )
        assert tight.stop_distance == 3.0
        assert tight.shares == int(125 // 3)


class TestStopDistance:
    def test_absolute_distance(self, engine: DeterministicRiskEngine) -> None:
        assert engine.stop_distance(100.0, 95.0) == 5.0

    def test_rejects_zero_distance(self, engine: DeterministicRiskEngine) -> None:
        with pytest.raises(ValueError):
            engine.stop_distance(100.0, 100.0)


class TestDrawdown:
    def test_drawdown_pct(self, engine: DeterministicRiskEngine) -> None:
        assert engine.drawdown_pct(100_000, 92_000) == pytest.approx(8.0)

    def test_no_negative_drawdown(self, engine: DeterministicRiskEngine) -> None:
        assert engine.drawdown_pct(100_000, 110_000) == 0.0


class TestSectorExposure:
    def test_includes_additional_notional(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        pct = engine.sector_exposure_pct(
            portfolio.positions, portfolio.equity, "Index", additional_notional=2_500
        )
        # 5000 + 2500 = 7500 / 25000 = 30%
        assert pct == pytest.approx(30.0)


class TestHardVetoes:
    def test_daily_loss_halts(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        portfolio.daily_pnl_pct = -1.6
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert result.approved is False
        assert result.halt_day is True
        assert VetoCode.DAILY_LOSS_LIMIT.value in result.hard_vetoes

    def test_max_drawdown_halts(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        portfolio.peak_equity = 100_000
        portfolio.equity = 91_000
        portfolio.drawdown_pct = 0  # engine recomputes from peak
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(price=480, stop=474),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert result.approved is False
        assert VetoCode.MAX_DRAWDOWN.value in result.hard_vetoes

    def test_missing_stop_blocks_buy(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        trade = _buy(stop=None)
        trade.invalidation = None
        result = engine.evaluate_pretrade(
            portfolio,
            trade,
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert result.approved is False
        assert VetoCode.MISSING_STOP.value in result.hard_vetoes

    def test_low_data_quality_blocks(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(),
            allowlist={"QQQ"},
            data_quality_score=0.3,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert result.approved is False
        assert VetoCode.LOW_DATA_QUALITY.value in result.hard_vetoes

    def test_not_in_allowlist(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(symbol="GME"),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert VetoCode.NOT_IN_ALLOWLIST.value in result.hard_vetoes

    def test_sell_exempt_from_allowlist(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        result = engine.evaluate_pretrade(
            portfolio,
            TradeIntent(
                symbol="CORZ",
                side="sell",
                quantity=11,
                entry_price=20.0,
                stop_loss=None,
                sector="Technology",
                avg_daily_volume=5_000_000,
                bid_ask_spread_bps=5,
                expected_slippage_bps=5,
            ),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert VetoCode.NOT_IN_ALLOWLIST.value not in result.hard_vetoes

    def test_penny_stock_blocked(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(symbol="QQQ", price=3.0, stop=2.5, qty=100),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert VetoCode.PENNY_STOCK.value in result.hard_vetoes

    def test_broker_mismatch_fail_closed(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=False,
        )
        assert result.approved is False
        assert VetoCode.BROKER_DATA_MISMATCH.value in result.hard_vetoes

    def test_five_losses_halt_day(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        portfolio.consecutive_losses = 5
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert result.halt_day is True
        assert VetoCode.CONSECUTIVE_LOSSES_HALT.value in result.hard_vetoes

    def test_cooldown_blocks_new_buys(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        portfolio.consecutive_losses = 3
        portfolio.cooldown_until = datetime.now(UTC) + timedelta(minutes=20)
        result = engine.evaluate_pretrade(
            portfolio,
            _buy(),
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
            now=datetime.now(UTC),
        )
        assert result.approved is False
        assert VetoCode.CONSECUTIVE_LOSSES_COOLDOWN.value in result.hard_vetoes


class TestIdempotency:
    def test_duplicate_key_rejected(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        trade = _buy()
        trade.idempotency_key = "premarket-qqq-001"
        result = engine.evaluate_pretrade(
            portfolio,
            trade,
            allowlist={"QQQ"},
            data_quality_score=1.0,
            market_session_clear=True,
            broker_data_consistent=True,
            seen_idempotency_keys={"premarket-qqq-001"},
        )
        assert result.approved is False
        assert "duplicate_idempotency_key" in result.hard_vetoes


class TestApprovedPath:
    def test_valid_trade_approved(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        # Small notional so cash/exposure stay within limits
        trade = _buy(qty=2, price=100.0, stop=98.0, symbol="MSFT", sector="Technology")
        portfolio.positions = []
        portfolio.gross_exposure_pct = 0.0
        portfolio.cash = 25_000
        portfolio.cash_pct = 100.0
        result = engine.evaluate_pretrade(
            portfolio,
            trade,
            allowlist={"MSFT"},
            data_quality_score=0.95,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert result.approved is True
        assert result.hard_vetoes == []
        assert result.adjusted_quantity is not None
        assert result.adjusted_quantity > 0


class TestVenueGrossCap:
    def test_venue_cap_blocks_same_book(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        portfolio.equity = 100_000
        portfolio.cash = 50_000
        portfolio.cash_pct = 50.0
        portfolio.gross_exposure_pct = 45.0
        portfolio.positions = [
            PositionRiskView(
                symbol="BHP",
                quantity=100,
                market_value=45_000,
                sector="Materials",
                weight_pct=45.0,
                venue="AU",
                currency="AUD",
            )
        ]
        trade = _buy(qty=100, price=100.0, stop=95.0, symbol="CBA", sector="Financials")
        result = engine.evaluate_pretrade(
            portfolio,
            trade,
            allowlist={"CBA"},
            data_quality_score=0.95,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert result.approved is False
        assert VetoCode.MAX_VENUE_GROSS_EXPOSURE.value in result.hard_vetoes

    def test_other_venue_does_not_count(
        self, engine: DeterministicRiskEngine, portfolio: PortfolioRiskView
    ) -> None:
        portfolio.equity = 100_000
        portfolio.cash = 60_000
        portfolio.cash_pct = 60.0
        portfolio.gross_exposure_pct = 40.0
        portfolio.positions = [
            PositionRiskView(
                symbol="AAPL",
                quantity=100,
                market_value=40_000,
                sector="Technology",
                weight_pct=40.0,
                venue="US",
                currency="USD",
            )
        ]
        trade = _buy(qty=50, price=100.0, stop=95.0, symbol="BHP", sector="Materials")
        result = engine.evaluate_pretrade(
            portfolio,
            trade,
            allowlist={"BHP"},
            data_quality_score=0.95,
            market_session_clear=True,
            broker_data_consistent=True,
        )
        assert VetoCode.MAX_VENUE_GROSS_EXPOSURE.value not in result.hard_vetoes
