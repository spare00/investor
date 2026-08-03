"""Persist pause / emergency-stop across process restarts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.safety_controls import TradingControlState, TradingControls, TradingState
from app.models import ConfigurationHistory

OPS_KEY = "ops.trading_controls"


async def persist_trading_controls(
    session: AsyncSession,
    controls: TradingControls,
    *,
    changed_by: str = "api",
) -> None:
    snap = controls.snapshot()
    payload = {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "canceled_open_orders": snap.canceled_open_orders,
    }
    latest = (
        await session.execute(
            select(ConfigurationHistory)
            .where(ConfigurationHistory.key == OPS_KEY)
            .order_by(ConfigurationHistory.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    old = latest.new_value if latest else None
    session.add(
        ConfigurationHistory(
            id=uuid4(),
            key=OPS_KEY,
            old_value=old,
            new_value=json.dumps(payload),
            changed_by=changed_by,
            reason=snap.reason,
        )
    )
    await session.flush()


async def restore_trading_controls(
    session: AsyncSession, controls: TradingControls
) -> dict[str, Any] | None:
    latest = (
        await session.execute(
            select(ConfigurationHistory)
            .where(ConfigurationHistory.key == OPS_KEY)
            .order_by(ConfigurationHistory.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return None
    try:
        data = json.loads(latest.new_value)
    except json.JSONDecodeError:
        return None
    state = TradingState(data.get("state", TradingState.ACTIVE.value))
    reason = data.get("reason")
    # Apply without going through resume (which cannot clear emergency).
    with controls._lock:  # noqa: SLF001 — intentional restore path
        controls._state = TradingControlState(
            state=state,
            reason=reason,
            changed_at=datetime.fromisoformat(data["changed_at"])
            if data.get("changed_at")
            else datetime.now(UTC),
            canceled_open_orders=bool(data.get("canceled_open_orders")),
        )
    return data
