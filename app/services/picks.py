"""Compact pick ledger — selected vs rejected names, with reasons.

Briefing shows full agent materials. This view is the one-line history:
which names Quant presented, what CIO did, and why a name was dropped
or blocked before it became a live order.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import theses_from_quant
from app.brokers.models import IntentStatus
from app.core.timeutils import dual_timezone_labels
from app.models import AgentReport, AgentRun, CIODecisionRecord, OrderIntent
from app.schemas.common import (
    AgentName,
    LiquidityState,
    MomentumState,
    TrendState,
    VolatilityState,
)
from app.schemas.quant_strategist import QuantStrategistOutput
from app.services.briefing import session_day_bounds_utc
from app.universe.book_strategy import horizon_for_symbol, should_propose_entry, tape_from_view

_ENTRY_ACTIONS = {"STRONG_BUY", "BUY", "SCALE_IN"}
_EXIT_ACTIONS = {"SELL", "PARTIAL_SELL", "REDUCE"}
_TRADE_ACTIONS = _ENTRY_ACTIONS | _EXIT_ACTIONS
_BLOCKED_INTENT = {
    IntentStatus.RISK_REJECTED.value,
    IntentStatus.REJECTED.value,
    IntentStatus.FAILED.value,
}
_LEDGER_AGENTS = {
    AgentName.QUANT_STRATEGIST.value,
    AgentName.RISK_MANAGER.value,
    AgentName.DEVILS_ADVOCATE.value,
}


def _clip(text: str | None, limit: int = 240) -> str:
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + "…"


def _enum(cls: type, raw: object) -> Any | None:
    if raw is None:
        return None
    if isinstance(raw, cls):
        return raw
    try:
        return cls(str(raw))
    except ValueError:
        return None


def _action(plan: dict[str, Any]) -> str:
    raw = plan.get("action")
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw) or "")


def _symbol(plan: dict[str, Any]) -> str:
    return str(plan.get("symbol") or "").upper()


def _vetoes_for(symbol: str, vetoes: list[Any]) -> list[str]:
    hits: list[str] = []
    needle = symbol.upper()
    for item in vetoes:
        text = str(item or "")
        if needle and needle in text.upper():
            hits.append(text)
    return hits


def _proposed_from_dicts(payload: dict[str, Any], regime: str | None) -> dict[str, str]:
    """Fallback when persisted Quant JSON no longer validates as the schema."""
    out: dict[str, str] = {}
    for view in payload.get("symbol_views") or []:
        if not isinstance(view, dict):
            continue
        if not view.get("entry_zone"):
            continue
        sym = str(view.get("symbol") or "").upper()
        if not sym:
            continue
        trend = _enum(TrendState, view.get("trend_state"))
        momentum = _enum(MomentumState, view.get("momentum_state"))
        liquidity = _enum(LiquidityState, view.get("liquidity_state"))
        vol = _enum(VolatilityState, view.get("volatility_state"))
        if None in {trend, momentum, liquidity, vol}:
            continue
        try:
            prob = float(view.get("probability_estimate") or 0.0)
        except (TypeError, ValueError):
            continue
        hz = horizon_for_symbol(sym)
        if not should_propose_entry(
            horizon=hz,
            probability=prob,
            trend=trend,
            momentum=momentum,
            liquidity=liquidity,
            volatility=vol,
            rsi=None,
            regime=regime,
            **tape_from_view(view),
        ):
            continue
        notes = [str(n) for n in (view.get("notes") or []) if n]
        basis = str(view.get("probability_basis") or "")
        out[sym] = notes[0] if notes else (basis or f"Quant {hz} p={prob:.2f}")
    return out


def proposed_from_quant(payload: dict[str, Any] | None, regime: str | None) -> dict[str, str]:
    """Symbol → Quant proposal summary (same gate Devil/CIO sees)."""
    if not payload:
        return {}
    try:
        quant = QuantStrategistOutput.model_validate(payload)
    except Exception:  # noqa: BLE001 — ledger must tolerate older dumps
        return _proposed_from_dicts(payload, regime)
    theses = theses_from_quant(
        quant,
        entry_universe=None,
        regime=regime,
        watchlist=None,
        limit=12,
    )
    out: dict[str, str] = {}
    for thesis in theses:
        sym = str(thesis.symbol or "").upper()
        if not sym:
            continue
        out[sym] = thesis.summary
    return out


def _intent_reason(intent: OrderIntent) -> str:
    meta = intent.metadata_json or {}
    rejections = meta.get("validation_rejections") or []
    if isinstance(rejections, list) and rejections:
        return _clip(", ".join(str(x) for x in rejections[:5]))
    if intent.thesis:
        return _clip(intent.thesis)
    return intent.status


def _blocked_intent(intents: list[OrderIntent], symbol: str) -> OrderIntent | None:
    hits = [i for i in intents if str(i.symbol or "").upper() == symbol]
    blocked = [i for i in hits if i.status in _BLOCKED_INTENT]
    if blocked:
        return blocked[0]
    return None


def rows_for_decision(
    record: CIODecisionRecord,
    *,
    quant_payload: dict[str, Any] | None,
    risk_payload: dict[str, Any] | None,
    devil_payload: dict[str, Any] | None,
    intents: list[OrderIntent],
) -> list[dict[str, Any]]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    plans = [p for p in (payload.get("symbol_actions") or []) if isinstance(p, dict)]
    by_sym = {_symbol(p): p for p in plans if _symbol(p)}
    regime = record.market_regime or payload.get("market_regime")
    proposed = proposed_from_quant(quant_payload, str(regime) if regime else None)
    risk = risk_payload or {}
    devil = devil_payload or {}
    vetoes = list(risk.get("hard_vetoes") or [])
    devil_no = bool(devil.get("prefer_no_trade"))
    devil_why = _clip(
        devil.get("prefer_no_trade_rationale") or devil.get("strongest_reason_thesis_is_wrong")
    )
    book_why = _clip(record.reason_not_to_trade or payload.get("reason_not_to_trade"))
    venue = str(payload.get("venue") or "").upper() or None
    when = record.decision_timestamp
    base = {
        "timestamp": when.isoformat() if when else None,
        "display": dual_timezone_labels(when) if when else None,
        "venue": venue,
        "portfolio_action": record.portfolio_action,
        "risk_approval": record.risk_approval,
        "decision_id": str(record.decision_id),
        "workflow_id": str(record.workflow_id) if record.workflow_id else None,
    }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        *,
        symbol: str,
        status: str,
        action: str,
        reason: str,
        source: str,
        horizon: str | None = None,
    ) -> None:
        key = f"{symbol}:{status}:{action}"
        if key in seen:
            return
        seen.add(key)
        plan = by_sym.get(symbol) or {}
        hz = horizon or plan.get("universe_horizon") or plan.get("time_horizon") or ""
        rows.append(
            {
                **base,
                "symbol": symbol,
                "status": status,
                "action": action or "—",
                "horizon": str(hz or ""),
                "reason": _clip(reason) or "—",
                "source": source,
            }
        )

    for plan in plans:
        sym = _symbol(plan)
        if not sym:
            continue
        action = _action(plan)
        thesis = str(plan.get("thesis") or plan.get("invalidation") or "")
        blocked = _blocked_intent(intents, sym)
        if action in _TRADE_ACTIONS:
            if blocked is not None:
                why = _intent_reason(blocked)
                add(
                    symbol=sym,
                    status="rejected",
                    action=action,
                    reason=(
                        f"CIO {action}: {thesis}. Blocked: {why}"
                        if thesis
                        else f"Blocked: {why}"
                    ),
                    source="execution",
                )
                continue
            add(
                symbol=sym,
                status="selected",
                action=action,
                reason=thesis or f"CIO {action}",
                source="cio",
            )
            continue
        why = thesis or book_why or action or record.portfolio_action
        add(
            symbol=sym,
            status="hold",
            action=action or "HOLD",
            reason=why,
            source="cio",
        )

    selected = {r["symbol"] for r in rows if r["status"] == "selected"}
    decided = {r["symbol"] for r in rows}

    for adj in risk.get("trade_adjustments") or []:
        if not isinstance(adj, dict):
            continue
        sym = str(adj.get("symbol") or "").upper()
        verdict = str(adj.get("verdict") or "").lower()
        if not sym or verdict not in {"rejected", "halt_day"}:
            continue
        if sym in decided:
            continue
        reasons = adj.get("reasons") or []
        why = ", ".join(str(x) for x in reasons[:4]) if isinstance(reasons, list) else str(reasons)
        add(
            symbol=sym,
            status="rejected",
            action="BUY",
            reason=why or f"risk {verdict}",
            source="risk",
        )

    for sym, summary in proposed.items():
        if sym in decided or sym in selected:
            continue
        plan = by_sym.get(sym) or {}
        hold_act = _action(plan) in {"HOLD", "NO_TRADE", "STAY_CASH", ""}
        hold_thesis = str(plan.get("thesis") or "") if hold_act else ""
        hits = _vetoes_for(sym, vetoes)
        if hits:
            why, source = hits[0], "risk"
        elif hold_thesis:
            why, source = hold_thesis, "cio"
        elif devil_no and devil_why:
            why, source = devil_why, "devil"
        elif book_why:
            why, source = book_why, "cio"
        else:
            why, source = f"Quant proposed; CIO did not take — {summary}", "quant"
        add(
            symbol=sym,
            status="rejected",
            action=_action(plan) or "HOLD",
            reason=why,
            source=source,
        )

    if not rows:
        why = book_why or (devil_why if devil_no else "") or record.portfolio_action
        book_hold = record.portfolio_action in {"NO_TRADE", "STAY_CASH", "HOLD"}
        empty_buy = record.portfolio_action in {"SCALE_IN", "BUY", "STRONG_BUY"}
        add(
            symbol="—",
            status="rejected" if book_hold or empty_buy else "selected",
            action=record.portfolio_action,
            reason=why or record.portfolio_action,
            source="cio",
        )

    return rows


class PicksService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build(
        self,
        *,
        session_date: str | None = None,
        venue: str | None = None,
        calendar_name: str = "NYSE",
        limit: int = 30,
    ) -> dict[str, Any]:
        cap = max(1, min(int(limit or 30), 80))
        stmt = select(CIODecisionRecord).order_by(desc(CIODecisionRecord.decision_timestamp))
        if session_date:
            start, end = session_day_bounds_utc(session_date, calendar_name=calendar_name)
            stmt = stmt.where(
                CIODecisionRecord.decision_timestamp >= start,
                CIODecisionRecord.decision_timestamp < end,
            )
            stmt = stmt.limit(120)
        else:
            stmt = stmt.limit(min(120, cap * 4))
        decisions = list((await self.session.execute(stmt)).scalars().all())
        book = (venue or "").upper() or None
        if book in {"US", "AU"}:
            decisions = [d for d in decisions if _decision_venue(d) in {None, book}]
        decisions = decisions[:cap]

        wf_ids = [d.workflow_id for d in decisions if d.workflow_id]
        agent_map = await self._agent_payloads(wf_ids)
        intent_map = await self._intents_by_decision([d.decision_id for d in decisions])

        rows: list[dict[str, Any]] = []
        for rec in decisions:
            agents = agent_map.get(rec.workflow_id or rec.decision_id, {})
            rows.extend(
                rows_for_decision(
                    rec,
                    quant_payload=agents.get(AgentName.QUANT_STRATEGIST.value),
                    risk_payload=agents.get(AgentName.RISK_MANAGER.value),
                    devil_payload=agents.get(AgentName.DEVILS_ADVOCATE.value),
                    intents=intent_map.get(rec.decision_id, []),
                )
            )

        selected = sum(1 for r in rows if r["status"] == "selected")
        rejected = sum(1 for r in rows if r["status"] == "rejected")
        held = sum(1 for r in rows if r["status"] == "hold")
        return {
            "available": True,
            "venue": book,
            "session_date": session_date,
            "decision_count": len(decisions),
            "counts": {
                "selected": selected,
                "rejected": rejected,
                "hold": held,
                "rows": len(rows),
            },
            "rows": rows,
        }

    async def _agent_payloads(
        self, workflow_ids: list[UUID]
    ) -> dict[UUID, dict[str, dict[str, Any]]]:
        if not workflow_ids:
            return {}
        runs = list(
            (
                await self.session.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.workflow_id.in_(workflow_ids),
                        AgentRun.agent_name.in_(_LEDGER_AGENTS),
                    )
                    .order_by(desc(AgentRun.started_at))
                )
            )
            .scalars()
            .all()
        )
        run_ids = [r.id for r in runs]
        reports: dict[UUID, AgentReport] = {}
        if run_ids:
            report_q = select(AgentReport).where(AgentReport.agent_run_id.in_(run_ids))
            for report in (await self.session.execute(report_q)).scalars().all():
                reports.setdefault(report.agent_run_id, report)
        out: dict[UUID, dict[str, dict[str, Any]]] = {}
        for run in runs:
            bucket = out.setdefault(run.workflow_id, {})
            if run.agent_name in bucket:
                continue
            report = reports.get(run.id)
            if report is None or not isinstance(report.payload, dict):
                continue
            bucket[run.agent_name] = report.payload
        return out

    async def _intents_by_decision(
        self, decision_ids: list[UUID]
    ) -> dict[UUID, list[OrderIntent]]:
        if not decision_ids:
            return {}
        rows = list(
            (
                await self.session.execute(
                    select(OrderIntent).where(OrderIntent.decision_id.in_(decision_ids))
                )
            )
            .scalars()
            .all()
        )
        out: dict[UUID, list[OrderIntent]] = {}
        for intent in rows:
            if intent.decision_id is None:
                continue
            out.setdefault(intent.decision_id, []).append(intent)
        return out


def _decision_venue(record: CIODecisionRecord) -> str | None:
    payload = record.payload if isinstance(record.payload, dict) else {}
    raw = payload.get("venue")
    if raw:
        return str(raw).upper()
    try:
        from app.market.venues import venue_for_symbol
    except Exception:  # noqa: BLE001
        return None
    for plan in payload.get("symbol_actions") or []:
        if not isinstance(plan, dict):
            continue
        sym = str(plan.get("symbol") or "").upper()
        if not sym:
            continue
        try:
            return venue_for_symbol(sym).value
        except Exception:  # noqa: BLE001
            return None
    return None
