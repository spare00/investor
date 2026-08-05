"""Universe / watchlist API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.models import Position
from app.universe.service import UniverseService
from sqlalchemy import select

router = APIRouter(prefix="/universe", tags=["universe"])


@router.get("")
async def universe_snapshot(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    return await UniverseService(session, settings=get_settings()).snapshot()


@router.post("/refresh")
async def universe_refresh(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    settings = get_settings()
    holdings = [
        p.symbol
        for p in (await session.execute(select(Position))).scalars().all()
    ]
    svc = UniverseService(session, settings=settings)
    result = await svc.refresh(holdings=holdings)
    await session.commit()
    return result


@router.get("/horizons")
async def universe_horizons() -> dict[str, Any]:
    from app.universe.horizons import all_horizon_summaries

    return {"horizons": all_horizon_summaries()}
