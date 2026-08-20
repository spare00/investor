"""ASX marketable-limit conversion."""

from __future__ import annotations

import pytest

from app.brokers.venue_orders import (
    aggressive_limit_price,
    apply_marketable_limit,
    is_sane_equity_price,
    uses_marketable_limit,
)


def test_au_and_asx_use_marketable_limit() -> None:
    assert uses_marketable_limit("AU") is True
    assert uses_marketable_limit(None, "ASX") is True
    assert uses_marketable_limit("US", "SMART") is False


def test_sell_limit_is_through_the_bid() -> None:
    px = aggressive_limit_price(side="sell", last=100.0)
    assert px < 100.0
    assert px >= 96.0


def test_buy_limit_is_through_the_ask() -> None:
    px = aggressive_limit_price(side="buy", last=100.0)
    assert px > 100.0
    assert px <= 104.0


def test_ibkr_unset_price_is_not_sane() -> None:
    assert is_sane_equity_price(None) is False
    assert is_sane_equity_price(0) is False
    assert is_sane_equity_price(1.7976931348623157e308) is False
    assert is_sane_equity_price(112.71) is True


def test_apply_never_leaves_au_market() -> None:
    otype, px = apply_marketable_limit(
        venue="AU",
        side="sell",
        order_type="market",
        limit_price=None,
        last=112.71,
        bid=112.68,
    )
    assert otype == "limit"
    assert px < 112.68


def test_apply_requires_a_tape() -> None:
    with pytest.raises(ValueError, match="asx_requires_reference_price"):
        apply_marketable_limit(
            venue="AU",
            side="sell",
            order_type="market",
            limit_price=None,
            last=None,
        )
