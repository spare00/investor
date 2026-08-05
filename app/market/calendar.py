"""US equity market calendar (NYSE) via exchange_calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CALENDAR_VERSION = "exchange_calendars:XNYS"


@dataclass(frozen=True, slots=True)
class MarketSessionInfo:
    session_date: date
    is_trading_day: bool
    is_early_close: bool
    premarket_start: datetime | None
    regular_open: datetime | None
    regular_close: datetime | None
    postmarket_end: datetime | None
    timezone: str
    calendar_source: str
    calendar_version: str

    def to_dict(self) -> dict[str, Any]:
        def _iso(v: datetime | None) -> str | None:
            return v.isoformat() if v else None

        return {
            "session_date": self.session_date.isoformat(),
            "is_trading_day": self.is_trading_day,
            "is_early_close": self.is_early_close,
            "premarket_start": _iso(self.premarket_start),
            "regular_open": _iso(self.regular_open),
            "regular_close": _iso(self.regular_close),
            "postmarket_end": _iso(self.postmarket_end),
            "timezone": self.timezone,
            "calendar_source": self.calendar_source,
            "calendar_version": self.calendar_version,
        }


@dataclass(frozen=True, slots=True)
class MarketStatusSnapshot:
    as_of: datetime
    as_of_us_eastern: str
    as_of_brisbane: str
    is_trading_day: bool
    phase: str
    session: MarketSessionInfo
    minutes_to_open: float | None
    minutes_to_close: float | None
    minutes_to_next_open: float | None
    next_open: datetime | None
    in_closing_window: bool
    in_force_close_window: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "as_of_us_eastern": self.as_of_us_eastern,
            "as_of_brisbane": self.as_of_brisbane,
            "is_trading_day": self.is_trading_day,
            "phase": self.phase,
            "session": self.session.to_dict(),
            "minutes_to_open": self.minutes_to_open,
            "minutes_to_close": self.minutes_to_close,
            "minutes_to_next_open": self.minutes_to_next_open,
            "next_open": self.next_open.isoformat() if self.next_open else None,
            "in_closing_window": self.in_closing_window,
            "in_force_close_window": self.in_force_close_window,
        }


def _ensure_aware(dt: datetime, default_tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=default_tz)
    return dt


class MarketCalendarService:
    """
    NYSE equity session calendar.

    Uses exchange_calendars (XNYS) so DST and holidays/early closes are data-driven,
    not hard-coded UTC offsets.
    """

    def __init__(self, settings: Settings | None = None, calendar_name: str | None = None) -> None:
        self.settings = settings or get_settings()
        name = calendar_name or self.settings.market_calendar
        # Map friendly name → exchange_calendars code
        code = {"NYSE": "XNYS", "NASDAQ": "XNAS", "XNYS": "XNYS", "XNAS": "XNAS"}.get(
            name.upper(), name.upper()
        )
        self.calendar_code = code
        self._cal = xcals.get_calendar(code)
        self.market_tz = ZoneInfo(self.settings.market_timezone)
        self.operator_tz = ZoneInfo(self.settings.operator_timezone)

    def _ts(self, day: date) -> pd.Timestamp:
        return pd.Timestamp(day)

    def is_trading_day(self, day: date) -> bool:
        return bool(self._cal.is_session(self._ts(day)))

    def get_session(self, day: date) -> MarketSessionInfo:
        if not self.is_trading_day(day):
            return MarketSessionInfo(
                session_date=day,
                is_trading_day=False,
                is_early_close=False,
                premarket_start=None,
                regular_open=None,
                regular_close=None,
                postmarket_end=None,
                timezone=str(self.market_tz),
                calendar_source=self.calendar_code,
                calendar_version=CALENDAR_VERSION,
            )
        ts = self._ts(day)
        open_utc = self._cal.session_open(ts).to_pydatetime()
        close_utc = self._cal.session_close(ts).to_pydatetime()
        open_et = open_utc.astimezone(self.market_tz)
        close_et = close_utc.astimezone(self.market_tz)
        # Regular close is 16:00 ET; early close typically 13:00 ET
        is_early = close_et.timetz().replace(tzinfo=None) < time(16, 0)
        premarket = datetime.combine(day, time(4, 0), tzinfo=self.market_tz)
        post_end = datetime.combine(day, time(20, 0), tzinfo=self.market_tz)
        return MarketSessionInfo(
            session_date=day,
            is_trading_day=True,
            is_early_close=is_early,
            premarket_start=premarket,
            regular_open=open_et,
            regular_close=close_et,
            postmarket_end=post_end,
            timezone=str(self.market_tz),
            calendar_source=self.calendar_code,
            calendar_version=CALENDAR_VERSION,
        )

    def get_next_trading_day(self, day: date) -> date:
        ts = self._ts(day)
        if self.is_trading_day(day):
            return self._cal.next_session(ts).date()
        return self._cal.date_to_session(ts, direction="next").date()

    def get_previous_trading_day(self, day: date) -> date:
        ts = self._ts(day)
        if self.is_trading_day(day):
            return self._cal.previous_session(ts).date()
        return self._cal.date_to_session(ts, direction="previous").date()

    def get_schedule(self, start_date: date, end_date: date) -> list[MarketSessionInfo]:
        if end_date < start_date:
            return []
        out: list[MarketSessionInfo] = []
        cur = start_date
        while cur <= end_date:
            out.append(self.get_session(cur))
            cur = cur + timedelta(days=1)
        return out

    def get_next_market_open(self, now: datetime) -> datetime | None:
        now = _ensure_aware(now, ZoneInfo("UTC")).astimezone(self.market_tz)
        day = now.date()
        # Search forward up to ~20 calendar days
        for _ in range(20):
            session = self.get_session(day)
            if session.is_trading_day and session.regular_open and now < session.regular_open:
                return session.regular_open
            day = day + timedelta(days=1)
        return None

    def get_next_market_close(self, now: datetime) -> datetime | None:
        now = _ensure_aware(now, ZoneInfo("UTC")).astimezone(self.market_tz)
        day = now.date()
        for _ in range(20):
            session = self.get_session(day)
            if session.is_trading_day and session.regular_close and now < session.regular_close:
                return session.regular_close
            day = day + timedelta(days=1)
        return None

    def get_market_status(self, now: datetime | None = None) -> MarketStatusSnapshot:
        now_utc = _ensure_aware(now or datetime.now(ZoneInfo("UTC")), ZoneInfo("UTC"))
        now_et = now_utc.astimezone(self.market_tz)
        session = self.get_session(now_et.date())
        phase = "NON_TRADING_DAY"
        minutes_to_open = None
        minutes_to_close = None
        in_closing = False
        in_force = False
        cfg = self.settings
        if session.is_trading_day and session.regular_open and session.regular_close:
            open_t = session.regular_open
            close_t = session.regular_close
            pre = session.premarket_start or open_t
            post = session.postmarket_end or close_t
            minutes_to_open = (open_t - now_et).total_seconds() / 60.0
            minutes_to_close = (close_t - now_et).total_seconds() / 60.0
            closing_mins = cfg.closing_window_minutes_before_close
            force_mins = cfg.force_close_before_market_close_minutes
            if now_et < pre:
                phase = "BEFORE_PREMARKET"
            elif pre <= now_et < open_t:
                phase = "PREMARKET"
            elif open_t <= now_et < close_t:
                phase = "REGULAR"
                if 0 <= minutes_to_close <= force_mins:
                    in_force = True
                    phase = "FORCE_CLOSE_WINDOW"
                elif 0 <= minutes_to_close <= closing_mins:
                    in_closing = True
                    phase = "CLOSING_WINDOW"
            elif close_t <= now_et < post:
                phase = "POSTMARKET"
            else:
                phase = "AFTER_HOURS"
            if minutes_to_open is not None and minutes_to_open < 0:
                minutes_to_open = None
            if minutes_to_close is not None and minutes_to_close < 0:
                minutes_to_close = None
        next_open = self.get_next_market_open(now_utc)
        minutes_to_next_open = None
        if next_open is not None:
            minutes_to_next_open = (next_open - now_et).total_seconds() / 60.0
            if minutes_to_next_open < 0:
                minutes_to_next_open = None
        return MarketStatusSnapshot(
            as_of=now_utc,
            as_of_us_eastern=now_et.isoformat(),
            as_of_brisbane=now_utc.astimezone(self.operator_tz).isoformat(),
            is_trading_day=session.is_trading_day,
            phase=phase,
            session=session,
            minutes_to_open=minutes_to_open,
            minutes_to_close=minutes_to_close,
            minutes_to_next_open=minutes_to_next_open,
            next_open=next_open,
            in_closing_window=in_closing,
            in_force_close_window=in_force,
        )
