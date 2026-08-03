"""Intraday / positions / closing / overnight / posttrade API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.intraday.service import IntradayService
from app.models import (
    IntradayDecisionRecord,
    IntradayEvent,
    PositionLifecycle,
    PostTradeReviewRecord,
)

router = APIRouter(tags=["intraday"])


class PricesBody(BaseModel):
    prices: dict[str, float] = Field(default_factory=dict)


class ExitPolicyBody(BaseModel):
    stop_price: float | None = None
    take_profit_targets: list[Any] | None = None


class ReduceBody(BaseModel):
    fraction: float = 0.5


@router.get("/intraday/status")
async def intraday_status(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    return IntradayService(session).status()


@router.get("/intraday/events")
async def list_events(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = await IntradayService(session).bus.list_events()
    return {
        "events": [
            {
                "event_id": str(e.id),
                "event_type": e.event_type,
                "status": e.status,
                "symbols": e.symbols,
                "priority": e.priority,
                "detected_at": e.detected_at.isoformat() if e.detected_at else None,
            }
            for e in rows
        ]
    }


@router.get("/intraday/events/{event_id}")
async def get_event(event_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    row = await session.get(IntradayEvent, event_id)
    if row is None:
        raise HTTPException(404, "event_not_found")
    return {
        "event_id": str(row.id),
        "event_type": row.event_type,
        "status": row.status,
        "payload": row.payload,
        "symbols": row.symbols,
    }


@router.post("/intraday/events/{event_id}/process")
async def process_event(event_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    row = await IntradayService(session).bus.mark(event_id, "PROCESSING")
    if row is None:
        raise HTTPException(404, "event_not_found")
    await IntradayService(session).bus.mark(event_id, "PROCESSED")
    await session.commit()
    return {"event_id": str(event_id), "status": "PROCESSED"}


@router.get("/intraday/decisions")
async def list_decisions(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = list(
        (await session.execute(select(IntradayDecisionRecord).order_by(IntradayDecisionRecord.as_of.desc()).limit(50)))
        .scalars()
        .all()
    )
    return {
        "decisions": [
            {
                "intraday_decision_id": str(d.id),
                "portfolio_action": d.portfolio_action,
                "thesis_status": d.thesis_status,
                "as_of": d.as_of.isoformat(),
            }
            for d in rows
        ]
    }


@router.post("/intraday/evaluate")
async def evaluate(fake_llm: bool = True, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await IntradayService(session).agents.evaluate(fake_llm=fake_llm)
    await session.commit()
    return result


@router.post("/intraday/recovery")
async def recovery(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await IntradayService(session).recovery.run()
    await session.commit()
    return result


@router.get("/positions/monitored")
async def monitored(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = await IntradayService(session).monitor.list_lifecycles()
    return {
        "positions": [
            {
                "position_id": str(p.id),
                "symbol": p.symbol,
                "status": p.status,
                "quantity": p.quantity,
                "stop_price": p.stop_price,
                "verdict": p.last_monitor_verdict,
            }
            for p in rows
        ]
    }


@router.get("/positions/{position_id}/snapshots")
async def snapshots(position_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    return {"snapshots": await IntradayService(session).snapshots(position_id)}


@router.get("/positions/{position_id}/risk")
async def position_risk(position_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    lc = await session.get(PositionLifecycle, position_id)
    if lc is None:
        raise HTTPException(404, "position_not_found")
    result = await IntradayService(session).risk.evaluate(
        lc, equity=25000, daily_pnl_pct=0, drawdown_pct=0, price=lc.current_price
    )
    await session.commit()
    return {"status": result.status, "reasons": result.reasons, "review_id": result.review_id}


@router.get("/positions/{position_id}/exit-policy")
async def exit_policy(position_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    lc = await session.get(PositionLifecycle, position_id)
    if lc is None:
        raise HTTPException(404, "position_not_found")
    return {"position_id": str(position_id), "exit_policy": lc.exit_policy, "stop_price": lc.stop_price}


@router.post("/positions/{position_id}/review")
async def review_position(
    position_id: UUID, body: PricesBody | None = None, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    lc = await session.get(PositionLifecycle, position_id)
    if lc is None:
        raise HTTPException(404, "position_not_found")
    prices = (body.prices if body else {}) or {}
    mon = await IntradayService(session).monitor.evaluate(
        lc, current_price=prices.get(lc.symbol), equity=25000
    )
    await session.commit()
    return {"monitor": {"verdict": mon.verdict, "reasons": mon.reasons}}


@router.post("/positions/{position_id}/reduce")
async def reduce_position(
    position_id: UUID, body: ReduceBody, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    try:
        result = await IntradayService(session).reduce_position(position_id, fraction=body.fraction)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await session.commit()
    return result


@router.post("/positions/{position_id}/close")
async def close_position(position_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    try:
        result = await IntradayService(session).close_position(position_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await session.commit()
    return result


@router.post("/positions/{position_id}/update-exit-policy")
async def update_exit_policy(
    position_id: UUID, body: ExitPolicyBody, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    try:
        result = await IntradayService(session).update_exit_policy(
            position_id, stop_price=body.stop_price, take_profit_targets=body.take_profit_targets
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await session.commit()
    return result


@router.post("/closing/run")
async def closing_run(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await IntradayService(session).closing.run_closing()
    await session.commit()
    return result


@router.post("/overnight/review")
async def overnight_review(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await IntradayService(session).closing.overnight_review()
    await session.commit()
    return result


@router.post("/postmarket/settle")
async def postmarket_settle(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await IntradayService(session).settlement.settle()
    await session.commit()
    return result


@router.get("/posttrade/reviews")
async def list_reviews(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = list(
        (await session.execute(select(PostTradeReviewRecord).order_by(PostTradeReviewRecord.created_at.desc()).limit(50)))
        .scalars()
        .all()
    )
    return {
        "reviews": [
            {"review_id": str(r.id), "symbol": r.symbol, "outcome": r.outcome, "pnl": r.pnl} for r in rows
        ]
    }


@router.get("/posttrade/reviews/{review_id}")
async def get_review(review_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    row = await session.get(PostTradeReviewRecord, review_id)
    if row is None:
        raise HTTPException(404, "review_not_found")
    return {
        "review_id": str(row.id),
        "symbol": row.symbol,
        "outcome": row.outcome,
        "exit_reason": row.exit_reason,
        "agent_assessment_ids": row.agent_assessment_ids,
    }


@router.post("/posttrade/review")
async def create_review(
    symbol: str,
    outcome: str = "closed",
    exit_reason: str = "manual",
    position_id: UUID | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await IntradayService(session).posttrade.create_review(
        position_lifecycle_id=position_id,
        symbol=symbol,
        outcome=outcome,
        exit_reason=exit_reason,
    )
    await session.commit()
    return result
