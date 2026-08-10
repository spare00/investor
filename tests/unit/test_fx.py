"""Static FX helper tests."""

from __future__ import annotations

import pytest

from app.market.fx import convert_amount, fx_rate, parse_fx_rates


def test_parse_fx_rates() -> None:
    assert parse_fx_rates("AUDUSD:0.65,EURUSD=1.1") == {"AUDUSD": 0.65, "EURUSD": 1.1}
    assert parse_fx_rates({"audusd": 0.7}) == {"AUDUSD": 0.7}


def test_fx_rate_direct_and_inverse() -> None:
    rates = parse_fx_rates("AUDUSD:0.65")
    assert fx_rate(rates, "AUD", "USD") == 0.65
    assert fx_rate(rates, "USD", "AUD") == pytest.approx(1.0 / 0.65)
    assert fx_rate(rates, "AUD", "AUD") == 1.0
    assert fx_rate(rates, "AUD", "EUR") is None


def test_convert_amount() -> None:
    rates = {"AUDUSD": 0.65}
    assert convert_amount(100.0, from_ccy="AUD", to_ccy="USD", rates=rates) == 65.0
