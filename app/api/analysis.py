"""API: analysis-only workflows (no broker orders). Phase 2 entrypoint."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import AgentPipeline
from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.logging import get_logger
from app.schemas.risk_manager import PortfolioStateInput
from app.services.collection import DataCollectionService
from app.services.llm import get_llm_client

logger = get_logger(__name__)
router = APIRouter(prefix="/workflow", tags=["workflow"])

# In-memory idempotency for analysis-only runs (process local).
_ANALYSIS_IDEMPOTENCY: dict[str, dict[str, Any]] = {}


def _analysis_payload(analysis: Any, collection: Any, *, broker_orders: bool = False) -> dict[str, Any]:
    return {
        "workflow_id": str(analysis.workflow_id),
        "broker_orders_submitted": broker_orders,
        "collection": {
            "aggregate_quality": collection.aggregate_quality,
            "fail_closed": collection.fail_closed,
            "news_count": len(collection.news),
            "market_count": len(collection.markets),
        },
        "market_intelligence": analysis.market_intelligence.model_dump(mode="json"),
        "macro": analysis.macro.model_dump(mode="json"),
        "quant": analysis.quant.model_dump(mode="json"),
        "risk": analysis.risk.model_dump(mode="json"),
        "devil": analysis.devil.model_dump(mode="json"),
        "cio": analysis.cio.model_dump(mode="json"),
        "completed_at": analysis.completed_at.isoformat(),
    }


@router.post("/premarket/analyze")
async def premarket_analyze(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """
    Collect premarket data and run the 6-agent bottom-up pipeline.

    Does not submit broker orders.
    """
    return await _run_analysis(session, idempotency_key=None)


@router.post("/analysis/run")
async def analysis_run(
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """
    Phase 2 analysis orchestration entrypoint.

    Bottom-up agent chain only — never submits broker orders.
    Optional Idempotency-Key header prevents duplicate work in-process.
    """
    if idempotency_key and idempotency_key in _ANALYSIS_IDEMPOTENCY:
        cached = _ANALYSIS_IDEMPOTENCY[idempotency_key]
        return {**cached, "idempotent_replay": True}
    result = await _run_analysis(session, idempotency_key=idempotency_key)
    if idempotency_key:
        _ANALYSIS_IDEMPOTENCY[idempotency_key] = result
    return {**result, "idempotent_replay": False}


async def _run_analysis(
    session: AsyncSession,
    *,
    idempotency_key: str | None,
) -> dict[str, Any]:
    settings = get_settings()
    workflow_id = uuid4()
    logger.info(
        "analysis_run_start",
        workflow_id=str(workflow_id),
        idempotency_key=idempotency_key,
    )
    collection = await DataCollectionService(session, persist=True).collect_premarket(
        workflow_id=workflow_id
    )
    llm = get_llm_client(settings)
    portfolio = PortfolioStateInput(
        as_of=datetime.now(UTC),
        equity=settings.starting_cash,
        cash=settings.starting_cash,
        cash_pct=100.0,
        gross_exposure_pct=0.0,
    )
    try:
        analysis = await AgentPipeline(settings=settings, llm=llm).run_from_collection(
            collection,
            portfolio=portfolio,
            proposed_trades=[],
            workflow_id=workflow_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("analysis_run_failed", workflow_id=str(workflow_id))
        raise HTTPException(status_code=500, detail=f"analysis_failed:{exc}") from exc

    payload = _analysis_payload(analysis, collection, broker_orders=False)
    payload["idempotency_key"] = idempotency_key
    return payload
