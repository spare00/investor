"""Workflow API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.decision.workflow import WorkflowService

router = APIRouter(prefix="/workflow", tags=["workflow"])


@router.post("/premarket/run")
async def premarket_run(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await WorkflowService(session).run_premarket()
    return result.to_dict()


@router.post("/intraday/evaluate")
async def intraday_evaluate(
    force: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await WorkflowService(session).run_intraday_evaluate(force=force)
    return result.to_dict()


@router.post("/postmarket/run")
async def postmarket_run(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await WorkflowService(session).run_postmarket()
    return result.to_dict()
