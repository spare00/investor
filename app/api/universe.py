"""Universe / watchlist API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.models import Position
from app.universe.service import UniverseService

router = APIRouter(prefix="/universe", tags=["universe"])


class UniverseRefreshRequest(BaseModel):
    themes: list[str] = Field(default_factory=list)
    market_regime: str | None = None
    # Bypass weekly min-interval (manual ops only — burns LLM budget).
    force: bool = False


@router.get("")
async def universe_snapshot(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    return await UniverseService(session, settings=get_settings()).snapshot()


@router.post("/refresh")
async def universe_refresh(
    body: UniverseRefreshRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    settings = get_settings()
    holdings = [
        p.symbol for p in (await session.execute(select(Position))).scalars().all()
    ]
    req = body or UniverseRefreshRequest()
    svc = UniverseService(session, settings=settings)
    result = await svc.refresh(
        holdings=holdings,
        market_regime=req.market_regime,
        themes=req.themes,
        force=bool(req.force),
    )
    replan: dict[str, Any] = {"skipped": True, "reason": "not_attempted"}
    try:
        from app.market.venues import enabled_venues
        from app.workflow.daily import DailyWorkflowService

        replan = {}
        for venue in enabled_venues(settings):
            replan[venue.value] = await DailyWorkflowService(
                session, settings=settings, venue=venue
            ).replan_intraday_jobs()
    except Exception as exc:  # noqa: BLE001
        replan = {"skipped": True, "reason": f"replan_failed:{exc}"}
    await session.commit()
    return {**result, "intraday_replan": replan}


@router.get("/horizons")
async def universe_horizons() -> dict[str, Any]:
    from app.universe.horizons import all_horizon_summaries

    return {"horizons": all_horizon_summaries()}
