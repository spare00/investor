"""Persist agent analysis / CIO decisions for dashboard & audit."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import AnalysisBundle
from app.models import AgentReport, AgentRun, CIODecisionRecord, RiskCheck, SystemEvent
from app.schemas.common import AgentName


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def persist_analysis(self, analysis: AnalysisBundle) -> None:
        wf = analysis.workflow_id
        now = datetime.now(UTC)

        async def _run(name: AgentName, payload: dict, quality: float | None = None) -> None:
            trace = payload.get("trace") or {}
            src_ts = trace.get("source_data_timestamp")
            if isinstance(src_ts, str):
                try:
                    src_ts = datetime.fromisoformat(src_ts.replace("Z", "+00:00"))
                except ValueError:
                    src_ts = None
            run = AgentRun(
                id=uuid4(),
                workflow_id=wf,
                agent_name=name.value,
                agent_version=str(trace.get("agent_version") or "0.1.0"),
                prompt_version=str(trace.get("prompt_version") or "0.1.0"),
                model_name=trace.get("model_name"),
                model_parameters=trace.get("model_parameters") or {},
                status="completed",
                started_at=now,
                finished_at=now,
                source_data_timestamp=src_ts,
                source_names=trace.get("source_names") or [],
            )
            self.session.add(run)
            await self.session.flush()
            self.session.add(
                AgentReport(
                    id=uuid4(),
                    agent_run_id=run.id,
                    report_type=name.value,
                    payload=payload,
                    data_quality_score=quality,
                )
            )

        await _run(
            AgentName.MARKET_INTELLIGENCE,
            analysis.market_intelligence.model_dump(mode="json"),
            analysis.market_intelligence.data_quality_score,
        )
        await _run(
            AgentName.MACRO_STRATEGIST,
            analysis.macro.model_dump(mode="json"),
            analysis.macro.data_quality_score,
        )
        await _run(
            AgentName.QUANT_STRATEGIST,
            analysis.quant.model_dump(mode="json"),
            analysis.quant.data_quality_score,
        )
        await _run(
            AgentName.RISK_MANAGER,
            analysis.risk.model_dump(mode="json"),
        )
        await _run(
            AgentName.DEVILS_ADVOCATE,
            analysis.devil.model_dump(mode="json"),
            analysis.devil.challenge_score,
        )
        await _run(
            AgentName.CIO,
            analysis.cio.model_dump(mode="json"),
        )

        cio = analysis.cio
        payload = cio.model_dump(mode="json")
        try:
            from app.universe.service import UniverseService

            hz_map = await UniverseService(self.session).horizon_by_symbol()
            for plan in payload.get("symbol_actions") or []:
                if not isinstance(plan, dict):
                    continue
                sym = str(plan.get("symbol") or "").upper()
                if sym and sym in hz_map:
                    plan["universe_horizon"] = hz_map[sym]
        except Exception:  # noqa: BLE001 — audit must not fail closed on horizon stamp
            pass
        self.session.add(
            CIODecisionRecord(
                id=uuid4(),
                decision_id=cio.decision_id,
                workflow_id=wf,
                decision_timestamp=cio.timestamp,
                market_regime=cio.market_regime.value,
                portfolio_action=cio.portfolio_action.value,
                payload=payload,
                risk_approval=cio.risk_approval,
                risk_conditions=list(cio.risk_conditions),
                reason_not_to_trade=cio.reason_not_to_trade,
                source_data_timestamp=(cio.trace.source_data_timestamp if cio.trace else None),
                agent_version=cio.trace.agent_version if cio.trace else "0.1.0",
                prompt_version=cio.trace.prompt_version if cio.trace else "0.1.0",
                model_name=cio.trace.model_name if cio.trace else None,
                model_parameters=cio.trace.model_parameters if cio.trace else {},
            )
        )

        self.session.add(
            RiskCheck(
                id=uuid4(),
                workflow_id=wf,
                decision_id=cio.decision_id,
                approved=analysis.risk.overall_verdict.value
                in {"approved", "conditional", "size_reduced"},
                halt_day=analysis.risk.halt_new_trades,
                hard_vetoes=list(analysis.risk.hard_vetoes),
                checks=list(analysis.risk.engine_checks),
                checked_at=now,
            )
        )
        await self.session.flush()

    async def record_event(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        context: dict | None = None,
        workflow_id: UUID | None = None,
    ) -> SystemEvent:
        row = SystemEvent(
            id=uuid4(),
            level=level,
            event_type=event_type,
            message=message,
            context=context or {},
            workflow_id=workflow_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row
