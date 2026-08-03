"""API: collect + agent analysis (no order execution)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import AgentPipeline
from app.core.config import get_settings
from app.core.database import get_db_session
from app.schemas.risk_manager import PortfolioStateInput
from app.services.collection import DataCollectionService
from app.services.llm import StubLLMClient, get_llm_client

router = APIRouter(prefix="/workflow", tags=["workflow"])


@router.post("/premarket/analyze")
async def premarket_analyze(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """
    Collect premarket data and run the 6-agent bottom-up pipeline.

    Does not submit broker orders (Phase 6).
    """
    settings = get_settings()
    collection = await DataCollectionService(session, persist=True).collect_premarket()
    llm = get_llm_client(settings)
    # If key missing, get_llm_client already returns StubLLMClient.
    if isinstance(llm, StubLLMClient) and not llm.payload:
        # Force fallbacks rather than empty JSON validation loops in API demos.
        pass

    portfolio = PortfolioStateInput(
        as_of=datetime.now(UTC),
        equity=settings.starting_cash,
        cash=settings.starting_cash,
        cash_pct=100.0,
        gross_exposure_pct=0.0,
    )
    analysis = await AgentPipeline(settings=settings, llm=llm).run_from_collection(
        collection,
        portfolio=portfolio,
        proposed_trades=[],
    )
    return {
        "workflow_id": str(analysis.workflow_id),
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
