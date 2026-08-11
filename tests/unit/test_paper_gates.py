"""Paper-mode relaxations for data fail-closed gates."""

from __future__ import annotations

from app.core.config import Settings, TradingMode
from app.market.live_prices import assess_collection_price_integrity
from app.market.paper_gates import paper_relaxed_data_gates, relax_fail_closed_reasons


def test_paper_relaxed_data_gates_only_on_paper() -> None:
    assert paper_relaxed_data_gates(
        Settings(trading_mode=TradingMode.PAPER, live_trading_enabled=False)
    )
    assert not paper_relaxed_data_gates(
        Settings(trading_mode=TradingMode.LIVE, live_trading_enabled=True)
    )
    assert not paper_relaxed_data_gates(
        Settings(
            trading_mode=TradingMode.PAPER,
            live_trading_enabled=True,
            paper_relaxed_data_gates=True,
        )
    )


def test_relax_missing_core_index_on_paper_with_quotes() -> None:
    settings = Settings(trading_mode=TradingMode.PAPER, live_trading_enabled=False)
    reasons, warnings = relax_fail_closed_reasons(
        ["missing_core_index_data"],
        quote_count=12,
        settings=settings,
    )
    assert reasons == []
    assert warnings == ["paper_relaxed:missing_core_index_data"]


def test_relax_does_not_apply_on_live() -> None:
    settings = Settings(trading_mode=TradingMode.LIVE, live_trading_enabled=True)
    reasons, warnings = relax_fail_closed_reasons(
        ["missing_core_index_data"],
        quote_count=12,
        settings=settings,
    )
    assert reasons == ["missing_core_index_data"]
    assert warnings == []


def test_assess_collection_paper_allows_missing_provider_labels() -> None:
    settings = Settings(
        trading_mode=TradingMode.PAPER,
        live_trading_enabled=False,
        enable_broker_orders=True,
        enable_external_data=True,
        broker_provider="ibkr",
    )
    live_req, feed_live, providers, notes = assess_collection_price_integrity(
        providers=[],
        market_count=8,
        settings=settings,
    )
    assert live_req is True
    assert feed_live is True
    assert "paper_relaxed_price_feed" in notes
