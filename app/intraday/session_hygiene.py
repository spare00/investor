"""Expire leftover session events, orphan hard-stop intents, and stale ops alerts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.intraday.events import MONITOR_EXECUTED_EVENT_TYPES
from app.models import (
    AlertRecordModel,
    IntradayEvent,
    OrderIntent,
    PositionLifecycle,
)

logger = get_logger(__name__)

_SESSION_MARKER_EVENTS = frozenset({"MARKET_CLOSED", "CLOSING_WINDOW_ENTERED"})
_OPEN_LC = ("OPEN", "PENDING_OPEN", "ADDING", "REDUCING", "PENDING_CLOSE")


def committee_allowed_for_phase(phase: str, *, in_force_close: bool, in_closing: bool) -> bool:
    """CIO reanalysis only during the venue's regular tape — not after the close."""
    return phase == "REGULAR" and not in_force_close and not in_closing


async def held_symbols(session: AsyncSession) -> set[str]:
    rows = list(
        (
            await session.execute(
                select(PositionLifecycle.symbol).where(PositionLifecycle.status.in_(_OPEN_LC))
            )
        )
        .scalars()
        .all()
    )
    return {str(s).upper() for s in rows if s}


async def fold_session_residue(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    phase: str | None = None,
    session_date: str | None = None,
) -> dict[str, int]:
    """Mark yesterday's NEW bus events / CREATED stops as done once the book is flat.

    Dashboard lists ``NEW`` events and ``CREATED`` hard-stop intents with no
    session filter, so a filled CBA stop stayed on the board all next day.
    """
    now = now or datetime.now(UTC)
    held = await held_symbols(session)
    out = {"events": 0, "intents": 0, "alerts": 0}

    events = list(
        (
            await session.execute(
                select(IntradayEvent).where(IntradayEvent.status.in_(["NEW", "QUEUED"]))
            )
        )
        .scalars()
        .all()
    )
    keep_session_markers = phase in {
        "POSTMARKET",
        "AFTER_HOURS",
        "FORCE_CLOSE_WINDOW",
        "CLOSING_WINDOW",
    }
    for ev in events:
        expires = ev.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        expired = expires is not None and expires <= now
        names = {str(s).upper() for s in (ev.symbols or []) if s}
        monitor_done = ev.event_type in MONITOR_EXECUTED_EVENT_TYPES and bool(names) and not (
            names & held
        )
        session_marker = ev.event_type in _SESSION_MARKER_EVENTS and not keep_session_markers
        if not (expired or monitor_done or session_marker):
            continue
        ev.status = "EXPIRED" if expired and not monitor_done else "PROCESSED"
        out["events"] += 1

    intents = list(
        (
            await session.execute(
                select(OrderIntent).where(OrderIntent.status == "CREATED")
            )
        )
        .scalars()
        .all()
    )
    for intent in intents:
        meta = intent.metadata_json if isinstance(intent.metadata_json, dict) else {}
        thesis = str(intent.thesis or "").lower()
        if meta.get("reason") != "hard_stop" and thesis != "hard_stop":
            continue
        if str(intent.symbol or "").upper() in held:
            continue
        intent.status = "EXPIRED"
        out["intents"] += 1

    out["alerts"] = await _fold_stale_alerts(
        session, held=held, session_date=session_date, now=now
    )
    if any(out.values()):
        await session.flush()
        logger.info("session_residue_folded", **out)
    return out


async def _fold_stale_alerts(
    session: AsyncSession,
    *,
    held: set[str],
    session_date: str | None,
    now: datetime,
) -> int:
    rows = list(
        (
            await session.execute(
                select(AlertRecordModel).where(AlertRecordModel.status.in_(["active", "acknowledged"]))
            )
        )
        .scalars()
        .all()
    )
    n = 0
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        ctx = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        code = str(row.alert_type or "")
        if code == "trading.hard_stop":
            sym = str(ctx.get("symbol") or row.symbol or "").upper()
            if not sym or sym in held:
                continue
            row.status = "resolved"
            row.resolved_at = now
            n += 1
            continue
        if code == "trading.overnight_review":
            sd = str(ctx.get("session_date") or "")
            flagged = ctx.get("flagged") if isinstance(ctx.get("flagged"), list) else []
            names = {str(r.get("symbol") or "").upper() for r in flagged if isinstance(r, dict)}
            past = bool(session_date and sd and sd < session_date)
            names_gone = bool(names) and not (names & held)
            if past or names_gone:
                row.status = "resolved"
                row.resolved_at = now
                n += 1
    if n:
        await session.flush()
    return n
