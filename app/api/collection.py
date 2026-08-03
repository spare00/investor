"""API routes for data collection (Phase 2)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.services.collection import DataCollectionService

router = APIRouter(prefix="/workflow", tags=["workflow"])


@router.post("/premarket/collect")
async def premarket_collect(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Collect/normalize/persist premarket inputs. Does not run agents or orders."""
    service = DataCollectionService(session, persist=True)
    bundle = await service.collect_premarket()
    return {
        "workflow_id": str(bundle.workflow_id),
        "collected_at": bundle.collected_at.isoformat(),
        "news_count": len(bundle.news),
        "market_count": len(bundle.markets),
        "macro_provider": bundle.macro.provider if bundle.macro else None,
        "earnings_count": len(bundle.earnings),
        "filings_count": len(bundle.filings),
        "aggregate_quality": bundle.aggregate_quality,
        "fail_closed": bundle.fail_closed,
        "eligible_symbols": [e.symbol for e in bundle.eligibility if e.eligible],
        "ineligible": [
            {"symbol": e.symbol, "reasons": list(e.reasons)}
            for e in bundle.eligibility
            if not e.eligible
        ],
        "errors": bundle.errors,
    }
