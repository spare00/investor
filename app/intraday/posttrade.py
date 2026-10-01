"""Post-trade review + agent evaluation references."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentOutcomeEvaluation, PositionLifecycle, PostTradeReviewRecord
from app.performance.trade_lesson import lesson_to_dict


class PostTradeReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_review(
        self,
        *,
        position_lifecycle_id: UUID | None = None,
        decision_id: UUID | None = None,
        symbol: str,
        outcome: str,
        exit_reason: str,
        pnl: float | None = None,
        thesis_status: str = "UNKNOWN",
        agent_runs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        universe_horizon = None
        lesson = None
        if position_lifecycle_id:
            lc_probe = await self.session.get(PositionLifecycle, position_lifecycle_id)
            if lc_probe:
                universe_horizon = (lc_probe.exit_policy or {}).get("horizon")
                try:
                    from app.performance.trade_lesson import judge_close

                    facts = await _close_facts(self.session, lc_probe)
                    lesson = judge_close(facts)
                except Exception:  # noqa: BLE001
                    lesson = None
                from app.performance.method_trial_store import complete_method_trial

                await complete_method_trial(self.session, lc_probe)

        stored_pnl = lesson.gross_pnl if lesson is not None else pnl
        lesson_payload = lesson_to_dict(lesson) if lesson is not None else None

        review = PostTradeReviewRecord(
            id=uuid4(),
            position_lifecycle_id=position_lifecycle_id,
            decision_id=decision_id,
            symbol=symbol,
            outcome=outcome,
            exit_reason=exit_reason,
            pnl=stored_pnl,
            entry_quality=None,
            execution_quality=None if lesson is None else lesson.execution_verdict,
            risk_adherence="unknown",
            exit_quality=None,
            thesis_accuracy=thesis_status,
            timing_quality=None,
            data_quality=None,
            what_worked=[],
            what_failed=[],
            avoidable_errors=[],
            unavoidable_factors=[],
            lessons=[] if lesson_payload is None else [lesson_payload],
            agent_assessment_ids=[],
            payload={
                "created_at": datetime.now(UTC).isoformat(),
                "universe_horizon": universe_horizon,
                "strategy_id": None if lesson is None else lesson.strategy_id,
                "counts_for_strategy": False if lesson is None else lesson.counts_for_strategy,
                "strategy_auto_changed": False,
            },
        )
        self.session.add(review)
        await self.session.flush()

        assessment_ids: list[str] = []
        for run in agent_runs or []:
            view = run.get("directional_view")
            actual_return = float(stored_pnl) if stored_pnl is not None else None
            direction_correct = None
            if actual_return is not None and view not in (None, "ABSTAIN", "NEUTRAL"):
                if view == "BULLISH":
                    direction_correct = actual_return > 0
                elif view == "BEARISH":
                    direction_correct = actual_return < 0
            ev = AgentOutcomeEvaluation(
                id=uuid4(),
                agent_name=str(run.get("agent_name") or "unknown"),
                agent_run_id=UUID(str(run["agent_run_id"])) if run.get("agent_run_id") else None,
                report_id=UUID(str(run["report_id"])) if run.get("report_id") else None,
                prediction_horizon=run.get("prediction_horizon"),
                directional_view=view,
                confidence=run.get("confidence"),
                key_claims=run.get("key_claims") or [],
                invalidation_conditions=run.get("invalidation_conditions") or [],
                actual_outcome_reference=str(review.id),
                evaluated_at=datetime.now(UTC) if actual_return is not None else None,
                payload={
                    "universe_horizon": universe_horizon,
                    "symbol": symbol.upper(),
                    "pnl": stored_pnl,
                    "outcome": outcome,
                    "actual_return": actual_return,
                    "direction_correct": direction_correct,
                },
            )
            self.session.add(ev)
            await self.session.flush()
            assessment_ids.append(str(ev.id))
        review.agent_assessment_ids = assessment_ids
        if position_lifecycle_id:
            lc = await self.session.get(PositionLifecycle, position_lifecycle_id)
            if lc and outcome in {"closed", "stopped", "take_profit"}:
                lc.status = "CLOSED"
                lc.closed_at = datetime.now(UTC)
        await self.session.flush()
        return {
            "review_id": str(review.id),
            "symbol": symbol,
            "outcome": outcome,
            "agent_assessment_ids": assessment_ids,
            "universe_horizon": universe_horizon,
            "strategy_auto_changed": False,
            "strategy_id": None if lesson is None else lesson.strategy_id,
            "execution_verdict": None if lesson is None else lesson.execution_verdict,
            "counts_for_strategy": False if lesson is None else lesson.counts_for_strategy,
        }


async def _close_facts(session: AsyncSession, lc: PositionLifecycle) -> Any:
    """Exit order plus the matching FIFO close. Missing pieces stay missing."""
    from sqlalchemy import select

    from app.intraday.fills import load_fill_records
    from app.intraday.pnl import reconstruct_fifo
    from app.models import Order
    from app.performance.trade_lesson import CloseFacts, intended_and_broker, select_exit_order
    from app.universe.entry_attribution import read_attribution

    symbol = str(lc.symbol or "").upper()
    orders = list(
        (await session.execute(select(Order).where(Order.symbol == symbol))).scalars().all()
    )
    sells = [
        {
            "idempotency_key": order.idempotency_key,
            "order_type": order.order_type,
            "status": order.status,
        }
        for order in orders
        if str(order.side or "").lower() == "sell"
    ]
    key, order_type = select_exit_order(sells)
    if key or order_type:
        intended, broker = intended_and_broker(key, order_type)
    else:
        intended, broker = None, None

    fills, _skipped = await load_fill_records(session, symbol=symbol)
    ledger = reconstruct_fifo(fills)
    gross = _gross_for_lifecycle(ledger.books, lc)
    policy = dict(lc.exit_policy or {})
    trial = dict(lc.metadata_json or {}).get("trial")
    trial = trial if isinstance(trial, dict) else {}
    horizon = trial.get("horizon") or policy.get("horizon")
    reason = trial.get("entry_reason") or read_attribution(lc).get("entry_timing")
    frozen_id = str(trial.get("strategy_id") or "") or None
    return CloseFacts(
        symbol=symbol,
        horizon=str(horizon) if horizon else None,
        entry_reason=str(reason) if reason else None,
        strategy_id=frozen_id,
        intended_order_type=intended,
        broker_order_type=broker,
        gross_pnl=gross,
    )


def _gross_for_lifecycle(books: list[Any], lc: PositionLifecycle) -> float | None:
    from app.intraday.pnl import as_utc

    symbol = str(lc.symbol or "").upper()
    known = [
        close
        for book in books
        if book.symbol == symbol
        for close in book.closes
        if close.gross_pnl is not None and close.closed_at is not None
    ]
    if not known or lc.closed_at is None:
        return None
    target = as_utc(lc.closed_at)
    best = min(known, key=lambda close: abs((as_utc(close.closed_at) - target).total_seconds()))
    if abs((as_utc(best.closed_at) - target).total_seconds()) > 24 * 3600:
        return None
    return float(best.gross_pnl)
