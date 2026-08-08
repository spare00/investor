"""Post-trade review + agent evaluation references."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentOutcomeEvaluation, PositionLifecycle, PostTradeReviewRecord


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
        if position_lifecycle_id:
            lc_probe = await self.session.get(PositionLifecycle, position_lifecycle_id)
            if lc_probe:
                universe_horizon = (lc_probe.exit_policy or {}).get("horizon")

        review = PostTradeReviewRecord(
            id=uuid4(),
            position_lifecycle_id=position_lifecycle_id,
            decision_id=decision_id,
            symbol=symbol,
            outcome=outcome,
            exit_reason=exit_reason,
            pnl=pnl,
            entry_quality=None,
            execution_quality=None,
            risk_adherence="unknown",
            exit_quality=None,
            thesis_accuracy=thesis_status,
            timing_quality=None,
            data_quality=None,
            what_worked=[],
            what_failed=[],
            avoidable_errors=[],
            unavoidable_factors=[],
            lessons=[],
            agent_assessment_ids=[],
            payload={
                "created_at": datetime.now(UTC).isoformat(),
                "universe_horizon": universe_horizon,
            },
        )
        self.session.add(review)
        await self.session.flush()

        assessment_ids: list[str] = []
        for run in agent_runs or []:
            ev = AgentOutcomeEvaluation(
                id=uuid4(),
                agent_name=str(run.get("agent_name") or "unknown"),
                agent_run_id=UUID(str(run["agent_run_id"])) if run.get("agent_run_id") else None,
                report_id=UUID(str(run["report_id"])) if run.get("report_id") else None,
                prediction_horizon=run.get("prediction_horizon"),
                directional_view=run.get("directional_view"),
                confidence=run.get("confidence"),
                key_claims=run.get("key_claims") or [],
                invalidation_conditions=run.get("invalidation_conditions") or [],
                actual_outcome_reference=str(review.id),
                evaluated_at=None,  # Phase 7 scores
                payload={
                    "universe_horizon": universe_horizon,
                    "symbol": symbol.upper(),
                    "pnl": pnl,
                    "outcome": outcome,
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
        }
