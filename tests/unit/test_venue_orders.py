"""ASX marketable-limit conversion."""

from __future__ import annotations

from app.brokers.venue_orders import aggressive_limit_price, uses_marketable_limit


def test_au_and_asx_use_marketable_limit() -> None:
    assert uses_marketable_limit("AU") is True
    assert uses_marketable_limit(None, "ASX") is True
    assert uses_marketable_limit("US", "SMART") is False


def test_sell_limit_is_through_the_bid() -> None:
    px = aggressive_limit_price(side="sell", last=100.0)
    assert px < 100.0
    assert px >= 99.0


def test_buy_limit_is_through_the_ask() -> None:
    px = aggressive_limit_price(side="buy", last=100.0)
    assert px > 100.0
    assert px <= 101.0
