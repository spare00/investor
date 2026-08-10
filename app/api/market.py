"""Market calendar / status API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from app.core.config import get_settings
from app.market.calendar import MarketCalendarService
from app.market.venues import Venue, parse_venue

router = APIRouter(prefix="/market", tags=["market"])


def _calendar(venue: str | None) -> MarketCalendarService:
    try:
        return MarketCalendarService(get_settings(), venue=parse_venue(venue))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/calendar")
async def market_calendar(
    start: date | None = None,
    end: date | None = None,
    day: date | None = None,
    venue: str | None = Query(default=None, description="US | AU"),
) -> dict[str, Any]:
    cal = _calendar(venue)
    if day is not None:
        return {"venue": cal.venue.value, "sessions": [cal.get_session(day).to_dict()]}
    if start is None:
        start = datetime.now(ZoneInfo(str(cal.market_tz))).date()
    if end is None:
        end = start
    return {
        "venue": cal.venue.value,
        "sessions": [s.to_dict() for s in cal.get_schedule(start, end)],
    }


@router.get("/status")
async def market_status(
    venue: str | None = Query(default=None, description="US | AU"),
) -> dict[str, Any]:
    return _calendar(venue).get_market_status().to_dict()


@router.get("/venues")
async def market_venues() -> dict[str, Any]:
    """List supported venues and current primary."""
    settings = get_settings()
    primary = parse_venue(settings.primary_venue) or Venue.US
    venues = []
    for v in Venue:
        spec = MarketCalendarService(settings, venue=v).venue_spec
        venues.append(
            {
                "venue": v.value,
                "mic": spec.mic,
                "calendar": spec.calendar_code,
                "timezone": spec.timezone,
                "currency": spec.currency,
            }
        )
    return {"primary_venue": primary.value, "venues": venues}
