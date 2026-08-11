"""Daily CIO briefing — shape agent materials for ops reading."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import dual_timezone_labels
from app.models import (
    AgentReport,
    AgentRun,
    CIODecisionRecord,
    DailyWorkflowRun,
    IntradayAnalysisRun,
    IntradayDecisionRecord,
    Order,
)
from app.schemas.common import AgentName

_ET = ZoneInfo("America/New_York")

_AGENT_LABELS = {
    AgentName.MARKET_INTELLIGENCE.value: "Market Intelligence",
    AgentName.MACRO_STRATEGIST.value: "Macro Strategist",
    AgentName.QUANT_STRATEGIST.value: "Quant Strategist",
    AgentName.RISK_MANAGER.value: "Risk Manager",
    AgentName.DEVILS_ADVOCATE.value: "Devil's Advocate",
    AgentName.CIO.value: "CIO",
}


def session_day_bounds_utc(session_date: str) -> tuple[datetime, datetime]:
    """ET session calendar day → UTC [start, end)."""
    day = date.fromisoformat(session_date)
    start_et = datetime(day.year, day.month, day.day, tzinfo=_ET)
    end_et = start_et + timedelta(days=1)
    return start_et.astimezone(UTC), end_et.astimezone(UTC)


def summarize_mi(payload: dict[str, Any] | None) -> dict[str, Any]:
    p = payload or {}
    events = []
    for ev in (p.get("market_events") or [])[:12]:
        if not isinstance(ev, dict):
            continue
        events.append(
            {
                "headline": ev.get("headline"),
                "importance": ev.get("importance"),
                "sentiment": ev.get("sentiment"),
                "category": ev.get("category"),
                "symbols": ev.get("symbols") or [],
            }
        )
    return {
        "themes": list(p.get("top_market_themes") or [])[:10],
        "events": events,
        "conflicts": list(p.get("conflicts") or [])[:8],
        "missing_information": list(p.get("missing_information") or [])[:8],
        "data_quality_score": p.get("data_quality_score"),
    }


def summarize_macro(payload: dict[str, Any] | None) -> dict[str, Any]:
    p = payload or {}
    return {
        "market_regime": p.get("market_regime"),
        "confidence": p.get("confidence"),
        "bullish_factors": list(p.get("bullish_factors") or [])[:8],
        "bearish_factors": list(p.get("bearish_factors") or [])[:8],
        "invalidation_conditions": list(p.get("invalidation_conditions") or [])[:8],
        "data_quality_score": p.get("data_quality_score"),
    }


def summarize_quant(payload: dict[str, Any] | None) -> dict[str, Any]:
    p = payload or {}
    views = []
    for v in (p.get("symbol_views") or [])[:15]:
        if not isinstance(v, dict):
            continue
        entry = v.get("entry_zone") or {}
        views.append(
            {
                "symbol": v.get("symbol"),
                "trend": v.get("trend_state"),
                "momentum": v.get("momentum_state"),
                "volatility": v.get("volatility_state"),
                "probability": v.get("probability_estimate"),
                "entry_zone": entry if isinstance(entry, dict) else None,
                "stop": v.get("stop_or_invalidation"),
                "notes": list(v.get("notes") or [])[:3],
            }
        )
    return {
        "market_trend_state": p.get("market_trend_state"),
        "market_momentum_state": p.get("market_momentum_state"),
        "market_volatility_state": p.get("market_volatility_state"),
        "market_breadth_state": p.get("market_breadth_state"),
        "market_liquidity_state": p.get("market_liquidity_state"),
        "symbol_views": views,
        "data_quality_score": p.get("data_quality_score"),
    }


def summarize_risk(payload: dict[str, Any] | None) -> dict[str, Any]:
    p = payload or {}
    return {
        "overall_verdict": p.get("overall_verdict"),
        "halt_new_trades": p.get("halt_new_trades"),
        "hard_vetoes": list(p.get("hard_vetoes") or [])[:12],
        "soft_warnings": list(p.get("soft_warnings") or [])[:12],
        "cash_pct": p.get("cash_pct"),
        "gross_exposure_pct": p.get("gross_exposure_pct"),
    }


def summarize_devil(payload: dict[str, Any] | None) -> dict[str, Any]:
    p = payload or {}
    return {
        "strongest_reason_thesis_is_wrong": p.get("strongest_reason_thesis_is_wrong"),
        "opposing_market_scenario": p.get("opposing_market_scenario"),
        "prefer_no_trade": p.get("prefer_no_trade"),
        "prefer_no_trade_rationale": p.get("prefer_no_trade_rationale"),
        "challenge_score": p.get("challenge_score"),
        "recommendation": p.get("recommendation"),
        "immediate_withdrawal_conditions": list(p.get("immediate_withdrawal_conditions") or [])[:8],
        "missing_information": list(p.get("missing_information") or [])[:8],
    }


def summarize_cio(payload: dict[str, Any] | None) -> dict[str, Any]:
    p = payload or {}
    actions = []
    for a in (p.get("symbol_actions") or [])[:20]:
        if not isinstance(a, dict):
            continue
        actions.append(
            {
                "symbol": a.get("symbol"),
                "action": a.get("action"),
                "confidence": a.get("confidence"),
                "target_position_pct": a.get("target_position_pct"),
                "time_horizon": a.get("time_horizon"),
                "thesis": a.get("thesis"),
                "invalidation": a.get("invalidation"),
                "stop_loss": a.get("stop_loss"),
            }
        )
    return {
        "portfolio_action": p.get("portfolio_action"),
        "market_regime": p.get("market_regime"),
        "cash_target_pct": p.get("cash_target_pct"),
        "risk_approval": p.get("risk_approval"),
        "risk_conditions": list(p.get("risk_conditions") or [])[:12],
        "reason_not_to_trade": p.get("reason_not_to_trade"),
        "hedge_required": p.get("hedge_required"),
        "symbol_actions": actions,
    }


_SUMMARIZERS = {
    AgentName.MARKET_INTELLIGENCE.value: summarize_mi,
    AgentName.MACRO_STRATEGIST.value: summarize_macro,
    AgentName.QUANT_STRATEGIST.value: summarize_quant,
    AgentName.RISK_MANAGER.value: summarize_risk,
    AgentName.DEVILS_ADVOCATE.value: summarize_devil,
    AgentName.CIO.value: summarize_cio,
}


def shape_agent_section(
    agent_name: str,
    *,
    payload: dict[str, Any] | None,
    run: AgentRun | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    summarizer = _SUMMARIZERS.get(agent_name, lambda p: p or {})
    section: dict[str, Any] = {
        "agent": agent_name,
        "label": _AGENT_LABELS.get(agent_name, agent_name),
        "present": payload is not None,
        "summary": summarizer(payload),
    }
    if run is not None:
        section["run"] = {
            "id": str(run.id),
            "status": run.status,
            "model_name": run.model_name,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "display": dual_timezone_labels(run.started_at) if run.started_at else None,
        }
    if include_raw and payload is not None:
        section["payload"] = payload
    return section


class BriefingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(
        self,
        *,
        session_date: str | None = None,
        include_raw: bool = False,
        calendar_name: str = "NYSE",
    ) -> dict[str, Any]:
        run = await self._resolve_daily_run(session_date, calendar_name=calendar_name)
        if run is None:
            return {
                "session_date": session_date,
                "available": False,
                "reason": "no_daily_workflow",
                "premarket": None,
                "intraday": [],
                "links": {},
            }

        wf_id = run.analysis_workflow_run_id or run.id
        agent_map = await self._agents_for_workflow(wf_id)
        # Manual dashboard "Intraday Eval" / WorkflowService uses a fresh workflow_id
        # that is not written to analysis_workflow_run_id — recover by session day.
        if len(agent_map) < 6:
            fallback_wf, fallback_map = await self._latest_agents_for_session(run.session_date)
            meta_probe = dict(run.metadata_json or {})
            linked = meta_probe.get("last_briefing_workflow_id")
            if linked:
                linked_map = await self._agents_for_workflow(UUID(str(linked)))
                if len(linked_map) > len(agent_map):
                    wf_id = UUID(str(linked))
                    agent_map = linked_map
            if len(fallback_map) > len(agent_map):
                wf_id = fallback_wf or wf_id
                agent_map = fallback_map

        cio_row = await self._cio_for_run(run)

        premarket_agents = []
        for name in (
            AgentName.MARKET_INTELLIGENCE.value,
            AgentName.MACRO_STRATEGIST.value,
            AgentName.QUANT_STRATEGIST.value,
            AgentName.RISK_MANAGER.value,
            AgentName.DEVILS_ADVOCATE.value,
            AgentName.CIO.value,
        ):
            pair = agent_map.get(name)
            payload = pair[1].payload if pair else None
            if name == AgentName.CIO.value and payload is None and cio_row is not None:
                payload = cio_row.payload
            premarket_agents.append(
                shape_agent_section(
                    name,
                    payload=payload if isinstance(payload, dict) else None,
                    run=pair[0] if pair else None,
                    include_raw=include_raw,
                )
            )

        found = sum(1 for a in premarket_agents if a["present"])
        session_analyses = await self._session_analyses(run.session_date, include_raw=include_raw)
        intraday = await self._intraday_for_session(run.session_date, include_raw=include_raw)

        meta = dict(run.metadata_json or {})
        risk_summary = next(
            (
                a["summary"]
                for a in premarket_agents
                if a["agent"] == AgentName.RISK_MANAGER.value and a["present"]
            ),
            None,
        ) or {}
        cio_summary = next(
            (
                a["summary"]
                for a in premarket_agents
                if a["agent"] == AgentName.CIO.value and a["present"]
            ),
            None,
        ) or {}
        # Prefer workflow meta, but fall back to agent materials so the strip
        # matches what operators see in the Risk / CIO cards below.
        latest_cio_action = (
            cio_summary.get("portfolio_action")
            or meta.get("cio_action")
        )
        risk_verdict = meta.get("risk_verdict") or risk_summary.get("overall_verdict")
        no_trade_reason = (
            meta.get("no_trade_reason")
            or cio_summary.get("reason_not_to_trade")
        )
        intent_count = meta.get("intent_count")
        if not intent_count and wf_id is not None:
            intent_count = await self._order_count_for_workflow(wf_id)
        return {
            "available": True,
            "session_date": run.session_date,
            "daily_workflow": {
                "id": str(run.id),
                "state": run.current_state,
                "status": run.status,
                "analysis_workflow_run_id": str(wf_id) if wf_id else None,
                "latest_decision_id": str(run.latest_decision_id) if run.latest_decision_id else None,
                "cio_action": latest_cio_action,
                "risk_verdict": risk_verdict,
                "no_trade_reason": no_trade_reason,
                "analysis_completed_at": meta.get("analysis_completed_at"),
                "intent_count": intent_count,
                "last_briefing_workflow_id": meta.get("last_briefing_workflow_id"),
                "last_briefing_kind": meta.get("last_briefing_kind"),
            },
            "premarket": {
                "workflow_id": str(wf_id),
                "agents": premarket_agents,
                "cio": cio_summary or next(
                    (a["summary"] for a in premarket_agents if a["agent"] == AgentName.CIO.value),
                    None,
                ),
            },
            "session_analyses": session_analyses,
            "intraday": intraday,
            "completeness": {
                "agent_reports_found": found,
                "agent_reports_expected": 6,
                "complete": found >= 6,
                "note": None
                if found >= 6
                else (
                    "Agent reports missing for this session — scheduled analysis may have "
                    "run before persistence was enabled, or analysis has not completed yet."
                ),
            },
            "links": {
                "daily_run_id": str(run.id),
                "premarket_workflow_id": str(wf_id),
                "latest_decision_id": str(run.latest_decision_id) if run.latest_decision_id else None,
                "latest_intraday_analysis_run_id": (intraday[0]["analysis_run_id"] if intraday else None),
                "latest_intraday_decision_id": (intraday[0]["id"] if intraday else None),
            },
        }

    async def _resolve_daily_run(
        self, session_date: str | None, *, calendar_name: str
    ) -> DailyWorkflowRun | None:
        if session_date:
            return (
                await self.session.execute(
                    select(DailyWorkflowRun).where(
                        DailyWorkflowRun.session_date == session_date,
                        DailyWorkflowRun.calendar_name == calendar_name,
                    )
                )
            ).scalar_one_or_none()
        return (
            await self.session.execute(
                select(DailyWorkflowRun)
                .where(DailyWorkflowRun.calendar_name == calendar_name)
                .order_by(desc(DailyWorkflowRun.session_date))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _agents_for_workflow(
        self, workflow_id: UUID
    ) -> dict[str, tuple[AgentRun, AgentReport]]:
        runs = list(
            (
                await self.session.execute(
                    select(AgentRun)
                    .where(AgentRun.workflow_id == workflow_id)
                    .order_by(desc(AgentRun.started_at))
                )
            )
            .scalars()
            .all()
        )
        out: dict[str, tuple[AgentRun, AgentReport]] = {}
        for run in runs:
            if run.agent_name in out:
                continue
            report = (
                await self.session.execute(
                    select(AgentReport).where(AgentReport.agent_run_id == run.id).limit(1)
                )
            ).scalar_one_or_none()
            if report is None:
                continue
            out[run.agent_name] = (run, report)
        return out

    async def _latest_agents_for_session(
        self, session_date: str
    ) -> tuple[UUID | None, dict[str, tuple[AgentRun, AgentReport]]]:
        start, end = session_day_bounds_utc(session_date)
        runs = list(
            (
                await self.session.execute(
                    select(AgentRun)
                    .where(AgentRun.started_at >= start, AgentRun.started_at < end)
                    .order_by(desc(AgentRun.started_at))
                )
            )
            .scalars()
            .all()
        )
        best_wf: UUID | None = None
        best_map: dict[str, tuple[AgentRun, AgentReport]] = {}
        seen: set[UUID] = set()
        for run in runs:
            if run.workflow_id in seen:
                continue
            seen.add(run.workflow_id)
            agent_map = await self._agents_for_workflow(run.workflow_id)
            if len(agent_map) > len(best_map):
                best_wf = run.workflow_id
                best_map = agent_map
            if len(best_map) >= 6:
                break
        return best_wf, best_map

    async def _order_count_for_workflow(self, workflow_id: UUID) -> int:
        """Count local orders keyed by workflow idempotency prefix."""
        prefix = f"{workflow_id}:"
        rows = list(
            (
                await self.session.execute(
                    select(Order).where(Order.idempotency_key.startswith(prefix))
                )
            )
            .scalars()
            .all()
        )
        return len(rows)

    async def _session_analyses(
        self, session_date: str, *, include_raw: bool
    ) -> list[dict[str, Any]]:
        """All distinct agent workflow bundles for the ET session day (newest first)."""
        start, end = session_day_bounds_utc(session_date)
        runs = list(
            (
                await self.session.execute(
                    select(AgentRun)
                    .where(AgentRun.started_at >= start, AgentRun.started_at < end)
                    .order_by(desc(AgentRun.started_at))
                )
            )
            .scalars()
            .all()
        )
        out: list[dict[str, Any]] = []
        seen: set[UUID] = set()
        for run in runs:
            if run.workflow_id in seen:
                continue
            seen.add(run.workflow_id)
            agent_map = await self._agents_for_workflow(run.workflow_id)
            if not agent_map:
                continue
            agents = [
                shape_agent_section(
                    name,
                    payload=agent_map[name][1].payload
                    if name in agent_map and isinstance(agent_map[name][1].payload, dict)
                    else None,
                    run=agent_map[name][0] if name in agent_map else None,
                    include_raw=include_raw,
                )
                for name in (
                    AgentName.MARKET_INTELLIGENCE.value,
                    AgentName.MACRO_STRATEGIST.value,
                    AgentName.QUANT_STRATEGIST.value,
                    AgentName.RISK_MANAGER.value,
                    AgentName.DEVILS_ADVOCATE.value,
                    AgentName.CIO.value,
                )
            ]
            started = max((a["run"]["started_at"] for a in agents if a.get("run")), default=None)
            cio_summary = next((a["summary"] for a in agents if a["agent"] == AgentName.CIO.value and a["present"]), None)
            out.append(
                {
                    "workflow_id": str(run.workflow_id),
                    "started_at": started,
                    "agents_present": sum(1 for a in agents if a["present"]),
                    "cio_action": (cio_summary or {}).get("portfolio_action"),
                    "market_regime": (cio_summary or {}).get("market_regime"),
                    "agents": agents,
                }
            )
            if len(out) >= 8:
                break
        return out

    async def _cio_for_run(self, run: DailyWorkflowRun) -> CIODecisionRecord | None:
        if run.latest_decision_id is not None:
            row = (
                await self.session.execute(
                    select(CIODecisionRecord)
                    .where(CIODecisionRecord.decision_id == run.latest_decision_id)
                    .order_by(desc(CIODecisionRecord.decision_timestamp))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                return row
        wf = run.analysis_workflow_run_id or run.id
        return (
            await self.session.execute(
                select(CIODecisionRecord)
                .where(CIODecisionRecord.workflow_id == wf)
                .order_by(desc(CIODecisionRecord.decision_timestamp))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _intraday_for_session(
        self, session_date: str, *, include_raw: bool
    ) -> list[dict[str, Any]]:
        start, end = session_day_bounds_utc(session_date)
        rows = list(
            (
                await self.session.execute(
                    select(IntradayDecisionRecord)
                    .where(
                        IntradayDecisionRecord.as_of >= start,
                        IntradayDecisionRecord.as_of < end,
                    )
                    .order_by(desc(IntradayDecisionRecord.as_of))
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            analysis = None
            agents: list[dict[str, Any]] = []
            if row.analysis_run_id is not None:
                analysis = (
                    await self.session.execute(
                        select(IntradayAnalysisRun).where(
                            IntradayAnalysisRun.id == row.analysis_run_id
                        )
                    )
                ).scalar_one_or_none()
                agent_map = await self._agents_for_workflow(row.analysis_run_id)
                for name in (
                    AgentName.MARKET_INTELLIGENCE.value,
                    AgentName.MACRO_STRATEGIST.value,
                    AgentName.QUANT_STRATEGIST.value,
                    AgentName.RISK_MANAGER.value,
                    AgentName.DEVILS_ADVOCATE.value,
                    AgentName.CIO.value,
                ):
                    pair = agent_map.get(name)
                    agents.append(
                        shape_agent_section(
                            name,
                            payload=pair[1].payload if pair and isinstance(pair[1].payload, dict) else None,
                            run=pair[0] if pair else None,
                            include_raw=include_raw,
                        )
                    )
            item: dict[str, Any] = {
                "id": str(row.id),
                "analysis_run_id": str(row.analysis_run_id) if row.analysis_run_id else None,
                "as_of": row.as_of.isoformat(),
                "display": dual_timezone_labels(row.as_of),
                "market_regime": row.market_regime,
                "thesis_status": row.thesis_status,
                "portfolio_action": row.portfolio_action,
                "risk_approval": row.risk_approval,
                "risk_conditions": list(row.risk_conditions or []),
                "symbol_actions": list(row.symbol_actions or [])[:20],
                "mode": (analysis.mode if analysis else None),
                "analysis_status": (analysis.status if analysis else None),
                "agents": agents,
                "agents_present": sum(1 for a in agents if a.get("present")),
            }
            if include_raw:
                item["payload"] = row.payload
            out.append(item)
        return out
