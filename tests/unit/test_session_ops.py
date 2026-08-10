"""Active session venue for manual dashboard ops."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings
from app.market.session_ops import resolve_active_session_venue
from app.market.venues import Venue


def test_active_venue_asx_daytime_bne() -> None:
    # Monday 2026-08-10 11:00 BNE = ASX regular; US still overnight.
    settings = Settings(enabled_venues=["US", "AU"], primary_venue="US")
    now = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)  # 11:00 BNE
    assert resolve_active_session_venue(settings, now=now) == Venue.AU


def test_active_venue_us_evening_bne() -> None:
    # Tuesday 2026-08-11 06:00 BNE ≈ Monday 16:00 ET — US regular.
    settings = Settings(enabled_venues=["US", "AU"], primary_venue="US")
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)  # 06:00 Tue BNE / 16:00 Mon ET
    assert resolve_active_session_venue(settings, now=now) == Venue.US


def test_active_venue_idle_weekend() -> None:
    # Sunday BNE — neither book in session cycle.
    settings = Settings(enabled_venues=["US", "AU"], primary_venue="US")
    now = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)  # Sun 14:00 BNE
    assert resolve_active_session_venue(settings, now=now) is None


def test_prefer_override() -> None:
    settings = Settings(enabled_venues=["US", "AU"], primary_venue="US")
    now = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)
    assert resolve_active_session_venue(settings, now=now, prefer="US") == Venue.US
    assert resolve_active_session_venue(settings, now=now, prefer="auto") == Venue.AU
