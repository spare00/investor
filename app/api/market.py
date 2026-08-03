"""Market calendar / status API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from app.core.config import get_settings
from app.market.calendar import MarketCalendarService

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/calendar")
async def market_calendar(
    start: date | None = None,
    end: date | None = None,
    day: date | None = None,
) -> dict[str, Any]:
    cal = MarketCalendarService(get_settings())
    if day is not None:
        return {"sessions": [cal.get_session(day).to_dict()]}
    if start is None:
        start = datetime.now(ZoneInfo(cal.settings.market_timezone)).date()
    if end is None:
        end = start
    return {"sessions": [s.to_dict() for s in cal.get_schedule(start, end)]}


@router.get("/status")
async def market_status() -> dict[str, Any]:
    return MarketCalendarService(get_settings()).get_market_status().to_dict()
