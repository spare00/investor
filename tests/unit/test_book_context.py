"""Venue book context for dual-market agent runs."""

from __future__ import annotations

from app.core.config import clear_settings_cache, get_settings
from app.market.book_context import build_venue_book_context, index_symbols_for_venue


def test_au_book_context(monkeypatch) -> None:
    monkeypatch.setenv("ENABLED_VENUES", "US,AU")
    monkeypatch.setenv("TRADE_ALLOWLIST_AU", "BHP,CBA,VAS")
    monkeypatch.setenv("PRIMARY_BENCHMARK_AU", "VAS")
    clear_settings_cache()
    book = build_venue_book_context(
        get_settings(), venue="AU", session_date="2026-08-10", phase="PREMARKET"
    )
    assert book.venue == "AU"
    assert book.currency == "AUD"
    assert book.benchmark == "VAS"
    assert "VAS" in book.index_symbols
    assert "BHP" in book.allowlist
    block = book.prompt_block()
    assert "BOOK AU" in block
    assert "AUD" in block


def test_us_index_symbols_default() -> None:
    clear_settings_cache()
    assert index_symbols_for_venue("US") == ("SPY", "QQQ", "IWM", "DIA")
