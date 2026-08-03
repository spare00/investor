"""Performance calculation orchestration — deterministic, no LLM, no strategy mutation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import (
    AgentEvaluationRecord,
    AgentOutcomeEvaluation,
    CIODecisionRecord,
    DailyPerformance,
    DecisionEvaluationRecord,
    PortfolioSnapshot,
    PositionLifecycle,
    TradePnL,
)
from app.performance.agent_eval import AgentPrediction, Direction, evaluate_agents
from app.performance.benchmarks import load_and_align
from app.performance.calibration import calibration_gap, expected_calibration_error
from app.performance.decision_eval import DecisionAction, evaluate_decision
from app.performance.drawdown import compute_drawdowns, current_drawdown, max_drawdown
from app.performance.execution_quality import compute_execution_quality
from app.performance.operational import aggregate_operational_kpis
from app.performance.providers import compute_provider_reliability
from app.performance.returns import (
    active_return,
    daily_returns,
    excess_return,
    portfolio_absolute_return,
    time_weighted_return,
)
from app.performance.risk import (
    alpha,
    annualized_volatility,
    beta,
    cagr,
    information_ratio,
    sharpe_ratio,
    sortino_ratio,
    tracking_error,
)
from app.performance.trades import ClosedTrade, compute_trade_metrics
from app.performance.types import CALCULATION_VERSION
from app.performance.valuation import build_portfolio_valuation


class PerformanceService:
    """Deterministic performance engine. Never auto-applies results to strategy."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._last_run: dict[str, Any] | None = None
        self._runs_by_key: dict[str, dict[str, Any]] = {}

    def _period_key(self, period_start: datetime, period_end: datetime) -> str:
        return f"{period_start.date().isoformat()}:{period_end.date().isoformat()}"

    def last_run_for_period(self, period_start: datetime, period_end: datetime) -> dict[str, Any] | None:
        return self._runs_by_key.get(self._period_key(period_start, period_end))

    async def recalculate(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        benchmark_name: str = "SPY",
        risk_free_rate: float = 0.0,
    ) -> dict[str, Any]:
        run = {
            "run_id": str(uuid4()),
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "calculation_version": CALCULATION_VERSION,
            "started_at": datetime.now(UTC).isoformat(),
            "benchmark": benchmark_name,
            "risk_free_rate": risk_free_rate,
            "status": "completed",
        }
        run["portfolio_summary"] = await self.portfolio_summary(period_start, period_end, benchmark_name=benchmark_name)
        run["returns"] = await self.returns_summary(period_start, period_end, benchmark_name=benchmark_name)
        run["risk"] = await self.risk_summary(period_start, period_end, benchmark_name=benchmark_name, risk_free_rate=risk_free_rate)
        run["drawdowns"] = await self.drawdowns(period_start, period_end)
        run["trades"] = await self.trade_metrics(period_start, period_end)
        run["completed_at"] = datetime.now(UTC).isoformat()
        self._last_run = run
        self._runs_by_key[self._period_key(period_start, period_end)] = run
        return run

    async def _equity_curve(self, period_start: datetime, period_end: datetime) -> list[tuple[datetime, float]]:
        result = await self.session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.as_of >= period_start)
            .where(PortfolioSnapshot.as_of <= period_end)
            .order_by(PortfolioSnapshot.as_of)
        )
        snaps = list(result.scalars().all())
        if snaps:
            return [(s.as_of, s.equity) for s in snaps]
        result = await self.session.execute(
            select(DailyPerformance).order_by(DailyPerformance.trade_date)
        )
        daily = list(result.scalars().all())
        curve: list[tuple[datetime, float]] = []
        for d in daily:
            dt = datetime.fromisoformat(d.trade_date)
            if period_start <= dt <= period_end:
                curve.append((dt, d.ending_equity))
        return curve

    async def portfolio_summary(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        benchmark_name: str = "SPY",
    ) -> dict[str, Any]:
        curve = await self._equity_curve(period_start, period_end)
        latest = await self.session.execute(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.as_of.desc()).limit(1)
        )
        snap = latest.scalar_one_or_none()
        valuation = None
        if snap:
            valuation = build_portfolio_valuation(
                portfolio_id="default",
                as_of=snap.as_of,
                valuation_kind="mark_to_market",
                cash=snap.cash,
                positions=snap.payload.get("positions", []) if snap.payload else [],
                source_snapshot_ids=[str(snap.id)],
            )
        bench = load_and_align(benchmark_name, curve) if curve else None
        return {
            "valuation": valuation,
            "equity_points": len(curve),
            "absolute_return": portfolio_absolute_return(curve),
            "benchmark_alignment": bench,
        }

    async def returns_summary(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        benchmark_name: str = "SPY",
    ) -> dict[str, Any]:
        curve = await self._equity_curve(period_start, period_end)
        rets = [r for _, r in daily_returns(curve)]
        port_daily = daily_returns(curve)
        bench = load_and_align(benchmark_name, curve)
        aligned = bench["aligned_returns"] if bench else None
        out: dict[str, Any] = {
            "time_weighted_return": time_weighted_return(rets),
            "observation_count": len(rets),
        }
        if aligned:
            out["excess_return"] = excess_return(port_daily, aligned, benchmark_name=benchmark_name)
            out["active_return"] = active_return(port_daily, aligned, benchmark_name=benchmark_name)
        else:
            out["excess_return"] = None
            out["active_return"] = None
        return out

    async def risk_summary(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        benchmark_name: str = "SPY",
        risk_free_rate: float = 0.0,
    ) -> dict[str, Any]:
        curve = await self._equity_curve(period_start, period_end)
        rets = [r for _, r in daily_returns(curve)]
        dd = max_drawdown(curve)
        bench = load_and_align(benchmark_name, curve)
        b_rets = [r for _, r in bench["aligned_returns"]] if bench and bench["aligned_returns"] else []
        years = max((period_end - period_start).total_seconds() / (365.25 * 86400), 1 / 365.25)
        start_eq = curve[0][1] if curve else 0.0
        end_eq = curve[-1][1] if curve else 0.0
        return {
            "cagr": cagr(start_eq, end_eq, years=years, period_start=period_start, period_end=period_end),
            "volatility": annualized_volatility(rets, period_start=period_start, period_end=period_end),
            "sharpe": sharpe_ratio(rets, risk_free_rate=risk_free_rate, period_start=period_start, period_end=period_end),
            "sortino": sortino_ratio(rets, risk_free_rate=risk_free_rate),
            "max_drawdown": dd,
            "beta": beta(rets, b_rets, benchmark_name=benchmark_name) if b_rets else None,
            "alpha": alpha(rets, b_rets, risk_free_rate=risk_free_rate, benchmark_name=benchmark_name) if b_rets else None,
            "tracking_error": tracking_error(rets, b_rets, benchmark_name=benchmark_name) if b_rets else None,
            "information_ratio": information_ratio(rets, b_rets, benchmark_name=benchmark_name) if b_rets else None,
        }

    async def drawdowns(self, period_start: datetime, period_end: datetime) -> dict[str, Any]:
        curve = await self._equity_curve(period_start, period_end)
        periods = compute_drawdowns(curve)
        return {
            "periods": [
                {
                    "peak_at": p.peak_at.isoformat(),
                    "trough_at": p.trough_at.isoformat(),
                    "drawdown_pct": p.drawdown_pct,
                    "duration_days": p.duration_days,
                    "recovery_days": p.recovery_days,
                    "status": p.status.value,
                }
                for p in periods
            ],
            "max_drawdown": max_drawdown(curve),
            "current_drawdown": current_drawdown(curve),
        }

    async def trade_metrics(self, period_start: datetime, period_end: datetime) -> dict[str, Any]:
        result = await self.session.execute(select(PositionLifecycle))
        lifecycles = list(result.scalars().all())
        trades: list[ClosedTrade] = []
        for lc in lifecycles:
            if lc.status != "CLOSED" or not lc.closed_at:
                continue
            if not (period_start <= lc.closed_at <= period_end):
                continue
            pnl = lc.realized_pl or 0.0
            holding = 0.0
            if lc.opened_at and lc.closed_at:
                holding = (lc.closed_at - lc.opened_at).total_seconds() / 60.0
            risk = None
            if lc.average_entry_price and lc.stop_price:
                risk = abs(lc.average_entry_price - lc.stop_price) * abs(lc.quantity)
            trades.append(ClosedTrade(pnl=pnl, holding_minutes=holding, risk_amount=risk))

        if not trades:
            result = await self.session.execute(select(TradePnL))
            for row in result.scalars().all():
                trades.append(ClosedTrade(pnl=row.net_realized_pl, holding_minutes=0.0, fees=row.fees))
        return compute_trade_metrics(trades)

    def execution(self, order_stats: dict[str, Any]) -> dict[str, Any]:
        return compute_execution_quality(order_stats)

    def decisions(
        self,
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            evaluate_decision(
                decision_price=d["decision_price"],
                action=d.get("action", DecisionAction.HOLD),
                horizon_price=d.get("horizon_price"),
                benchmark_return=d.get("benchmark_return"),
            )
            for d in decisions
        ]

    async def agents(self, period_start: datetime, period_end: datetime) -> dict[str, Any]:
        result = await self.session.execute(select(AgentOutcomeEvaluation))
        rows = list(result.scalars().all())
        predictions: list[AgentPrediction] = []
        for row in rows:
            if row.evaluated_at and not (period_start <= row.evaluated_at <= period_end):
                continue
            payload = row.payload or {}
            actual = payload.get("actual_return")
            abstained = row.directional_view in (None, "ABSTAIN", "NEUTRAL")
            predictions.append(
                AgentPrediction(
                    predicted_direction=row.directional_view or Direction.ABSTAIN,
                    confidence=row.confidence,
                    actual_return=actual,
                    abstained=abstained,
                )
            )
        return evaluate_agents(predictions)

    def calibration(self, samples: list[tuple[float, float]], *, min_sample_size: int = 5) -> dict[str, Any]:
        return {
            "calibration_gap": calibration_gap(samples, min_sample_size=min_sample_size),
            "ece": expected_calibration_error(samples, min_sample_size=min_sample_size),
        }

    def providers(self, stats: dict[str, Any]) -> dict[str, Any]:
        return compute_provider_reliability(stats)

    def operational(self, counters: dict[str, Any]) -> dict[str, Any]:
        return aggregate_operational_kpis(counters)

    async def calibration_samples(self) -> list[tuple[float, float]]:
        result = await self.session.execute(select(AgentOutcomeEvaluation))
        samples: list[tuple[float, float]] = []
        for row in result.scalars().all():
            if row.confidence is None:
                continue
            payload = row.payload or {}
            actual = payload.get("direction_correct")
            if actual is None:
                actual = payload.get("actual_return", 0) > 0
            samples.append((float(row.confidence), 1.0 if actual else 0.0))
        return samples

    async def evaluate_decisions_batch(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        limit: int = 50,
        persist: bool = False,
    ) -> dict[str, Any]:
        result = await self.session.execute(
            select(CIODecisionRecord)
            .where(CIODecisionRecord.decision_timestamp >= period_start)
            .where(CIODecisionRecord.decision_timestamp <= period_end)
            .limit(limit)
        )
        rows = list(result.scalars().all())
        evaluations: list[dict[str, Any]] = []
        for row in rows:
            payload = row.payload or {}
            price = float(payload.get("reference_price") or payload.get("decision_price") or 0.0)
            horizon_price = payload.get("horizon_price")
            bench_ret = payload.get("benchmark_return")
            ev = evaluate_decision(
                decision_price=price,
                action=payload.get("portfolio_action", row.portfolio_action),
                horizon_price=horizon_price,
                benchmark_return=bench_ret,
            )
            item = {
                "decision_id": str(row.decision_id),
                "action": row.portfolio_action,
                "evaluated_at": row.decision_timestamp.isoformat(),
                "metrics": {
                    k: (v.__dict__ if hasattr(v, "__dict__") else v) for k, v in ev.items()
                },
            }
            evaluations.append(item)
            if persist:
                self.session.add(
                    DecisionEvaluationRecord(
                        decision_id=row.decision_id,
                        decision_type="cio",
                        action=row.portfolio_action,
                        decision_price=price,
                        evaluated_at=datetime.now(UTC),
                        payload=item,
                        status="AVAILABLE",
                    )
                )
        if persist:
            await self.session.flush()
        return {"count": len(evaluations), "evaluations": evaluations}

    async def evaluate_agents_batch(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        persist: bool = False,
    ) -> dict[str, Any]:
        summary = await self.agents(period_start, period_end)
        if persist:
            result = await self.session.execute(select(AgentOutcomeEvaluation))
            for row in result.scalars().all():
                if row.evaluated_at and not (period_start <= row.evaluated_at <= period_end):
                    continue
                payload = row.payload or {}
                self.session.add(
                    AgentEvaluationRecord(
                        agent_name=row.agent_name,
                        agent_run_id=row.agent_run_id,
                        report_id=row.report_id,
                        prediction_horizon=row.prediction_horizon or "1d",
                        directional_view=row.directional_view,
                        confidence=row.confidence,
                        key_claims=row.key_claims,
                        risk_warnings=row.invalidation_conditions,
                        abstained=row.directional_view in (None, "ABSTAIN", "NEUTRAL"),
                        actual_outcome=row.actual_outcome_reference,
                        direction_correct=payload.get("direction_correct"),
                        evaluated_at=row.evaluated_at or datetime.now(UTC),
                        evaluation_status="EVALUATED",
                        payload=payload,
                    )
                )
            await self.session.flush()
        return summary
