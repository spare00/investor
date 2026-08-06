"""Universe refresh session gate + notes coercion (overnight LLM burn fixes)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.scheduler import _universe_refresh_allowed_now
from app.schemas.universe_manager import UniverseManagerOutput


def test_notes_string_coerced_to_list() -> None:
    out = UniverseManagerOutput.model_validate(
        {
            "timestamp": datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            "notes": "Watchlist stable; no adds.",
            "proposals": [],
            "focus_symbols": ["SPY"],
        }
    )
    assert out.notes == ["Watchlist stable; no adds."]


def test_notes_empty_string_becomes_empty_list() -> None:
    out = UniverseManagerOutput.model_validate(
        {
            "timestamp": datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            "notes": "   ",
        }
    )
    assert out.notes == []


def test_universe_refresh_skipped_before_premarket() -> None:
    # Wednesday 2026-08-05 02:30 ET = deep overnight before 04:00 premarket
    settings = Settings(
        universe_refresh_session_only=True,
        market_timezone="America/New_York",
    )
    now = datetime(2026, 8, 5, 6, 30, tzinfo=UTC)  # 02:30 ET
    assert _universe_refresh_allowed_now(settings, now) is False


def test_universe_refresh_allowed_during_premarket() -> None:
    settings = Settings(
        universe_refresh_session_only=True,
        market_timezone="America/New_York",
    )
    # 05:00 ET on a trading Wednesday
    now = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    assert now.astimezone(ZoneInfo("America/New_York")).hour == 5
    assert _universe_refresh_allowed_now(settings, now) is True


def test_universe_refresh_session_only_off_allows_overnight() -> None:
    settings = Settings(
        universe_refresh_session_only=False,
        market_timezone="America/New_York",
    )
    now = datetime(2026, 8, 5, 6, 30, tzinfo=UTC)
    assert _universe_refresh_allowed_now(settings, now) is True
