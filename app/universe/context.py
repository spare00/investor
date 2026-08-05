"""Load last known regime / themes for universe refresh context."""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentReport, AgentRun, CIODecisionRecord
from app.universe.candidates import themes_for_regime


async def load_last_regime_context(session: AsyncSession) -> dict[str, Any]:
    """Best-effort prior session context for Universe Manager ranking."""
    market_regime: str | None = None
    themes: list[str] = []

    cio = (
        await session.execute(
            select(CIODecisionRecord).order_by(desc(CIODecisionRecord.decision_timestamp)).limit(1)
        )
    ).scalar_one_or_none()
    if cio is not None and cio.market_regime:
        market_regime = str(cio.market_regime)

    mi = (
        await session.execute(
            select(AgentReport)
            .join(AgentRun, AgentReport.agent_run_id == AgentRun.id)
            .where(AgentRun.agent_name == "market_intelligence")
            .order_by(desc(AgentRun.started_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if mi is not None and isinstance(mi.payload, dict):
        raw = mi.payload.get("top_market_themes") or []
        if isinstance(raw, list):
            themes = [str(t).strip().lower() for t in raw if str(t).strip()]

    if not themes and market_regime:
        themes = themes_for_regime(market_regime)

    return {
        "market_regime": market_regime,
        "themes": themes,
        "source": "cio+mi" if cio or mi else "none",
    }
