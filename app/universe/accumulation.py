"""Stealth accumulation — multi-day same-price buying footprint.

Institutions cannot lift a name in one print without running the offer, so large
buy programs are split across sessions at similar prices. This module detects
that *proxy* from daily OHLCV. It is a follow signal for the short book, not a
claim that the tape is "easy money".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.market.venues import get_venue_spec, venue_for_symbol

MIN_SESSIONS = 4
MAX_SESSIONS = 8
# Closes cluster in a tight band (the "same price for several days" claim).
MAX_CLOSE_SPAN_PCT = 0.025
# Whole-window high-low still a box, not a trend day sequence.
MAX_RANGE_PCT = 0.05
# Sliding: the "bid" is actually leaking lower.
MIN_NET_DRIFT_PCT = -0.01
# One session that already jumped is not stealth.
MAX_SINGLE_DAY_PCT = 0.025
VOLUME_FLOOR_MULT = 0.85
MIN_VOLUME_DAYS = 3
SPIKE_VOL_MULT = 4.0
MIN_BUY_VOLUME_SHARE = 0.52


@dataclass(frozen=True, slots=True)
class SessionPrint:
    session_date: str
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None


@dataclass(frozen=True, slots=True)
class AccumulationHit:
    detected: bool
    reason: str
    sessions: int = 0
    close_span_pct: float | None = None
    range_pct: float | None = None
    net_drift_pct: float | None = None
    volume_days: int = 0
    buy_volume_share: float | None = None

    def brief(self) -> dict[str, Any] | None:
        if not self.detected:
            return None
        return {
            "n": self.sessions,
            "span": round(float(self.close_span_pct or 0.0), 4),
            "vol_days": self.volume_days,
            "buy_share": round(float(self.buy_volume_share or 0.0), 2),
        }


def session_date_for(as_of: datetime, symbol: str) -> str:
    """Venue-local calendar date for a snapshot timestamp."""
    ts = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    tz_name = get_venue_spec(venue_for_symbol(symbol)).timezone
    return ts.astimezone(ZoneInfo(tz_name)).date().isoformat()


def session_print_from_row(row: Any, *, session_date: str | None = None) -> SessionPrint | None:
    symbol = str(getattr(row, "symbol", "") or "").upper()
    as_of = getattr(row, "as_of", None)
    close = getattr(row, "last", None)
    if close is None:
        close = getattr(row, "close", None)
    try:
        close_f = float(close)
    except (TypeError, ValueError):
        return None
    if close_f <= 0:
        return None
    date = session_date
    if date is None and isinstance(as_of, datetime) and symbol:
        date = session_date_for(as_of, symbol)
    if not date:
        return None
    return SessionPrint(
        session_date=date,
        close=close_f,
        open=_opt_float(getattr(row, "open", None)),
        high=_opt_float(getattr(row, "high", None)),
        low=_opt_float(getattr(row, "low", None)),
        volume=_opt_float(getattr(row, "volume", None)),
    )


def collapse_snapshots_to_sessions(
    rows: Sequence[Any],
    *,
    symbol: str,
    max_sessions: int = MAX_SESSIONS,
) -> list[SessionPrint]:
    """Keep the latest print per venue-local session date, chronological."""
    by_date: dict[str, SessionPrint] = {}
    latest_as_of: dict[str, datetime] = {}
    for row in rows:
        as_of = getattr(row, "as_of", None)
        if not isinstance(as_of, datetime):
            continue
        ts = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        date = session_date_for(ts, symbol)
        prev_ts = latest_as_of.get(date)
        if prev_ts is not None and ts <= prev_ts:
            continue
        printed = session_print_from_row(row, session_date=date)
        if printed is None:
            continue
        by_date[date] = printed
        latest_as_of[date] = ts
    ordered = [by_date[k] for k in sorted(by_date)]
    return ordered[-max_sessions:]


def upsert_session(prints: list[SessionPrint], incoming: SessionPrint) -> list[SessionPrint]:
    """Replace today's session or append, keep chronological + cap."""
    out = [p for p in prints if p.session_date != incoming.session_date]
    out.append(incoming)
    out.sort(key=lambda p: p.session_date)
    return out[-MAX_SESSIONS:]


def view_has_accumulation(view: Any) -> bool:
    if view is None:
        return False
    if isinstance(view, dict):
        timing = view.get("entry_timing")
        notes = view.get("notes") or []
    else:
        timing = getattr(view, "entry_timing", None)
        notes = getattr(view, "notes", None) or []
    if str(timing or "").strip().lower() == "accumulation":
        return True
    for raw in notes:
        text = str(raw).strip().lower()
        if text.startswith("timing=accumulation") or text.startswith("stealth="):
            return True
    return False


def detect_stealth_accumulation(
    bars: Sequence[Any] | None,
    *,
    avg_volume: float | None = None,
) -> AccumulationHit:
    """True when closes cluster and volume persists without a directional run."""
    prints = [_coerce_print(b) for b in (bars or [])]
    prints = [p for p in prints if p is not None]
    if len(prints) < MIN_SESSIONS:
        return AccumulationHit(detected=False, reason="insufficient_history", sessions=len(prints))
    window = prints[-MAX_SESSIONS:]
    n = len(window)
    closes = [p.close for p in window]
    mid = _median(closes)
    if mid <= 0:
        return AccumulationHit(detected=False, reason="bad_price", sessions=n)

    close_span = (max(closes) - min(closes)) / mid
    highs = [_high(p) for p in window]
    lows = [_low(p) for p in window]
    range_pct = (max(highs) - min(lows)) / mid
    net_drift = (closes[-1] - closes[0]) / closes[0]

    if close_span > MAX_CLOSE_SPAN_PCT:
        return _miss("close_span", n, close_span, range_pct, net_drift)
    if range_pct > MAX_RANGE_PCT:
        return _miss("wide_range", n, close_span, range_pct, net_drift)
    if net_drift < MIN_NET_DRIFT_PCT:
        return _miss("sliding", n, close_span, range_pct, net_drift)

    for p in window:
        day_pct = _day_move_pct(p)
        if day_pct is not None and day_pct > MAX_SINGLE_DAY_PCT:
            return _miss("single_day_jump", n, close_span, range_pct, net_drift)

    if len(lows) >= 3 and lows[-1] <= min(lows[:-1]) * 0.997:
        return _miss("range_break", n, close_span, range_pct, net_drift)

    volumes = [p.volume for p in window if p.volume is not None and p.volume > 0]
    vol_days, buy_share, vol_reason = _volume_persistence(window, volumes, avg_volume)
    if vol_reason:
        return AccumulationHit(
            detected=False,
            reason=vol_reason,
            sessions=n,
            close_span_pct=close_span,
            range_pct=range_pct,
            net_drift_pct=net_drift,
            volume_days=vol_days,
            buy_volume_share=buy_share,
        )

    return AccumulationHit(
        detected=True,
        reason="stealth_split_buy",
        sessions=n,
        close_span_pct=close_span,
        range_pct=range_pct,
        net_drift_pct=net_drift,
        volume_days=vol_days,
        buy_volume_share=buy_share,
    )


def _miss(
    reason: str,
    n: int,
    close_span: float,
    range_pct: float,
    net_drift: float,
) -> AccumulationHit:
    return AccumulationHit(
        detected=False,
        reason=reason,
        sessions=n,
        close_span_pct=close_span,
        range_pct=range_pct,
        net_drift_pct=net_drift,
    )


def _volume_persistence(
    window: list[SessionPrint],
    volumes: list[float],
    avg_volume: float | None,
) -> tuple[int, float | None, str | None]:
    buy_share = _buy_volume_share(window)
    if not volumes:
        return 0, buy_share, "no_volume"
    median_vol = _median(volumes)
    if median_vol <= 0:
        return 0, buy_share, "no_volume"
    peak = max(volumes)
    if peak >= SPIKE_VOL_MULT * median_vol:
        spike_idx = max(range(len(window)), key=lambda i: window[i].volume or 0.0)
        spike = window[spike_idx]
        day_range = (_high(spike) - _low(spike)) / spike.close if spike.close else 0.0
        typical = _median([(_high(p) - _low(p)) / p.close for p in window if p.close])
        if typical > 0 and day_range >= 1.6 * typical:
            return 0, buy_share, "news_spike"

    if avg_volume and avg_volume > 0:
        floor = float(avg_volume) * VOLUME_FLOOR_MULT
    else:
        floor = median_vol * 0.85
    vol_days = sum(1 for p in window if p.volume is not None and p.volume >= floor)
    if vol_days < min(MIN_VOLUME_DAYS, len(window) - 1):
        return vol_days, buy_share, "volume_faded"
    if buy_share is not None and buy_share < MIN_BUY_VOLUME_SHARE:
        return vol_days, buy_share, "distribution"
    return vol_days, buy_share, None


def _buy_volume_share(window: list[SessionPrint]) -> float | None:
    up = 0.0
    down = 0.0
    prev_close: float | None = None
    for p in window:
        vol = p.volume or 0.0
        if vol <= 0:
            prev_close = p.close
            continue
        baseline = p.open if p.open is not None else prev_close
        prev_close = p.close
        if baseline is None:
            continue
        if p.close >= baseline:
            up += vol
        else:
            down += vol
    total = up + down
    if total <= 0:
        return None
    return up / total


def _day_move_pct(p: SessionPrint) -> float | None:
    if p.open is not None and p.open > 0:
        return abs(p.close - p.open) / p.open
    if p.high is not None and p.low is not None and p.low > 0:
        return (p.high - p.low) / p.close if p.close else None
    return None


def _high(p: SessionPrint) -> float:
    if p.high is not None:
        return p.high
    return max(p.close, p.open if p.open is not None else p.close)


def _low(p: SessionPrint) -> float:
    if p.low is not None:
        return p.low
    return min(p.close, p.open if p.open is not None else p.close)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_print(raw: Any) -> SessionPrint | None:
    if raw is None:
        return None
    if isinstance(raw, SessionPrint):
        return raw
    if isinstance(raw, dict):
        close = raw.get("close", raw.get("last"))
        date = str(raw.get("session_date") or raw.get("date") or "")
        open_ = raw.get("open")
        high = raw.get("high")
        low = raw.get("low")
        volume = raw.get("volume")
    else:
        from_row = session_print_from_row(raw)
        if from_row is not None:
            return from_row
        close = getattr(raw, "close", None)
        date = str(getattr(raw, "session_date", "") or getattr(raw, "date", "") or "")
        open_ = getattr(raw, "open", None)
        high = getattr(raw, "high", None)
        low = getattr(raw, "low", None)
        volume = getattr(raw, "volume", None)
    try:
        close_f = float(close)
    except (TypeError, ValueError):
        return None
    if close_f <= 0 or not date:
        return None
    return SessionPrint(
        session_date=date,
        close=close_f,
        open=_opt_float(open_),
        high=_opt_float(high),
        low=_opt_float(low),
        volume=_opt_float(volume),
    )
