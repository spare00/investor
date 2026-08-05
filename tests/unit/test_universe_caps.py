"""Horizon book capacity tests."""

from __future__ import annotations

from app.universe.caps import count_open_by_horizon, horizon_cap_violation


def test_horizon_cap_blocks_new_slot() -> None:
    horizons = {
        "SPY": "scalp",
        "QQQ": "scalp",
        "NVDA": "day",
    }
    # scalp max_positions = 2
    assert (
        horizon_cap_violation(
            symbol="IWM",
            horizon_by_symbol={**horizons, "IWM": "scalp"},
            held_symbols=["SPY", "QQQ"],
            is_new_symbol=True,
        )
        is not None
    )
    assert (
        horizon_cap_violation(
            symbol="NVDA",
            horizon_by_symbol=horizons,
            held_symbols=["SPY"],
            is_new_symbol=True,
        )
        is None
    )


def test_count_open_by_horizon() -> None:
    counts = count_open_by_horizon(
        ["SPY", "NVDA", "MSFT"],
        {"SPY": "scalp", "NVDA": "day", "MSFT": "short"},
    )
    assert counts == {"scalp": 1, "day": 1, "short": 1}
