"""Performance calculation orchestration — deterministic, no LLM, no strategy mutation."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import (
    AgentEvaluationRecord,
    AgentOutcomeEvaluation,
    AgentReport,
    AgentRun,
    CIODecisionRecord,
    DailyPerformance,
    DailyWorkflowRun,
    DecisionEvaluationRecord,
    PortfolioSnapshot,
    PositionLifecycle,
    TradePnL,
)
from app.performance.agent_eval import (
    AgentPrediction,
    Direction,
    evaluate_agents,
    evaluate_agents_grouped,
)
from app.performance.benchmarks import load_and_align
from app.performance.calibration import calibration_gap, expected_calibration_error
from app.performance.decision_eval import (
    DecisionAction,
    evaluate_decision,
    summarize_decision_evaluations,
    universe_horizon_for_plan,
)
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
from app.performance.trades import ClosedTrade, compute_trade_metrics, group_trade_metrics_by_horizon
from app.performance.types import CALCULATION_VERSION, MetricResult


def _jsonable(value: Any) -> Any:
    if isinstance(value, MetricResult) or (is_dataclass(value) and not isinstance(value, type)):
        raw = asdict(value) if is_dataclass(value) else value.__dict__
        out = {}
        for k, v in raw.items():
            if hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
                out[k] = v.value
            elif isinstance(v, datetime):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    return value


def _decision_book_venue(
    *,
    payload: dict[str, Any],
    workflow_id: Any,
    wf_venues: dict[Any, str],
    symbol: str | None,
    settings: Settings,
) -> str:
    """Resolve US/AU ops book for a CIO decision row (workflow → payload → symbol)."""
    from app.market.venues import resolve_venue, venue_for_symbol

    raw = payload.get("venue") or payload.get("book_venue")
    if raw:
        v = str(raw).upper()
        if v in {"US", "AU"}:
            return v
    if workflow_id is not None and workflow_id in wf_venues:
        return wf_venues[workflow_id]
    if symbol:
        return venue_for_symbol(symbol, settings).value
    return resolve_venue(settings).value


def _benchmark_for_venue(venue: str, settings: Settings) -> str:
    if str(venue).upper() == "AU":
        return str(settings.primary_benchmark_au or "VAS").upper()
    return str(settings.primary_benchmark or "SPY").upper()


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
        from app.models import WatchlistSymbol
        from app.universe.horizons import UniverseHorizon

        result = await self.session.execute(select(PositionLifecycle))
        lifecycles = list(result.scalars().all())
        wl = list((await self.session.execute(select(WatchlistSymbol))).scalars().all())
        watchlist_hz = {r.symbol.upper(): str(r.horizon) for r in wl if r.symbol}

        def _resolve_horizon(lc: PositionLifecycle) -> str:
            policy = dict(lc.exit_policy or {})
            raw = policy.get("horizon")
            if raw:
                try:
                    return UniverseHorizon(str(raw).lower()).value
                except ValueError:
                    pass
            sym = str(lc.symbol or "").upper()
            if sym in watchlist_hz:
                try:
                    return UniverseHorizon(watchlist_hz[sym].lower()).value
                except ValueError:
                    return "unknown"
            return "unknown"

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
            trades.append(
                ClosedTrade(
                    pnl=pnl,
                    holding_minutes=holding,
                    risk_amount=risk,
                    symbol=str(lc.symbol).upper(),
                    horizon=_resolve_horizon(lc),
                )
            )

        if not trades:
            result = await self.session.execute(select(TradePnL))
            for row in result.scalars().all():
                trades.append(
                    ClosedTrade(
                        pnl=row.net_realized_pl,
                        holding_minutes=0.0,
                        fees=row.fees,
                        symbol=str(getattr(row, "symbol", "") or "").upper() or None,
                        horizon="unknown",
                    )
                )
        firm = compute_trade_metrics(trades)
        firm["by_horizon"] = group_trade_metrics_by_horizon(trades)
        firm["unit"] = "position_lifecycle"
        firm["horizon_note"] = (
            "by_horizon uses lifecycle exit_policy.horizon with watchlist fallback"
        )
        return firm

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
            payload = dict(row.payload or {})
            actual = _actual_return_from_payload(payload)
            abstained = row.directional_view in (None, "ABSTAIN", "NEUTRAL")
            hz = payload.get("universe_horizon")
            if hz is not None:
                hz = str(hz).lower()
            predictions.append(
                AgentPrediction(
                    predicted_direction=row.directional_view or Direction.ABSTAIN,
                    confidence=row.confidence,
                    actual_return=actual,
                    abstained=abstained,
                    universe_horizon=hz,
                    agent_name=str(row.agent_name or "unknown"),
                )
            )
        firm = evaluate_agents(predictions)
        firm["by_horizon"] = evaluate_agents_grouped(predictions, by="universe_horizon")
        firm["by_agent"] = evaluate_agents_grouped(predictions, by="agent_name")
        firm["horizon_note"] = (
            "by_horizon uses AgentOutcomeEvaluation.payload.universe_horizon "
            "(from lifecycle exit_policy at post-trade)"
        )
        return firm

    def calibration(
        self,
        samples: list[tuple[float, float]],
        *,
        min_sample_size: int = 5,
        by_horizon: dict[str, list[tuple[float, float]]] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "calibration_gap": calibration_gap(samples, min_sample_size=min_sample_size),
            "ece": expected_calibration_error(samples, min_sample_size=min_sample_size),
            "sample_count": len(samples),
            "min_sample_size": min_sample_size,
        }
        if by_horizon is not None:
            out["by_horizon"] = {
                book: {
                    "calibration_gap": calibration_gap(items, min_sample_size=min_sample_size),
                    "ece": expected_calibration_error(items, min_sample_size=min_sample_size),
                    "sample_count": len(items),
                    "min_sample_size": min_sample_size,
                }
                for book, items in by_horizon.items()
            }
        return out

    def providers(self, stats: dict[str, Any]) -> dict[str, Any]:
        return compute_provider_reliability(stats)

    def operational(self, counters: dict[str, Any]) -> dict[str, Any]:
        return aggregate_operational_kpis(counters)

    async def calibration_samples(self) -> list[tuple[float, float]]:
        grouped = await self.calibration_samples_by_horizon()
        return list(grouped.get("_all") or [])

    async def calibration_samples_by_horizon(self) -> dict[str, list[tuple[float, float]]]:
        result = await self.session.execute(select(AgentOutcomeEvaluation))
        books = ("scalp", "day", "short", "medium", "unknown")
        by_horizon: dict[str, list[tuple[float, float]]] = {b: [] for b in books}
        all_samples: list[tuple[float, float]] = []
        for row in result.scalars().all():
            if row.confidence is None:
                continue
            payload = dict(row.payload or {})
            actual = payload.get("direction_correct")
            if actual is None:
                ret = _actual_return_from_payload(payload)
                if ret is None:
                    continue
                actual = ret > 0
            sample = (float(row.confidence), 1.0 if actual else 0.0)
            all_samples.append(sample)
            hz = str(payload.get("universe_horizon") or "unknown").lower()
            if hz not in by_horizon:
                hz = "unknown"
            by_horizon[hz].append(sample)
        by_horizon["_all"] = all_samples
        return by_horizon

    async def evaluate_decisions_batch(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        limit: int = 50,
        persist: bool = False,
        skip_evaluated: bool = False,
    ) -> dict[str, Any]:
        from app.models import WatchlistSymbol
        from app.performance.price_lookup import (
            DecisionPriceResolver,
            evaluation_horizon_delta,
            evaluation_horizon_label,
        )

        window = [
            CIODecisionRecord.decision_timestamp >= period_start,
            CIODecisionRecord.decision_timestamp <= period_end,
        ]
        done = (
            select(DecisionEvaluationRecord.decision_id)
            .where(DecisionEvaluationRecord.decision_type == "cio")
            .where(DecisionEvaluationRecord.decision_id.is_not(None))
        )
        q = select(CIODecisionRecord).where(*window)
        if skip_evaluated:
            q = q.where(CIODecisionRecord.decision_id.notin_(done))
        remaining_q = select(func.count()).select_from(CIODecisionRecord).where(*window)
        if skip_evaluated:
            remaining_q = remaining_q.where(CIODecisionRecord.decision_id.notin_(done))
        unevaluated = int(await self.session.scalar(remaining_q) or 0)
        result = await self.session.execute(
            q.order_by(desc(CIODecisionRecord.decision_timestamp)).limit(limit)
        )
        rows = list(result.scalars().all())
        remaining_after = max(0, unevaluated - len(rows))
        if persist and rows:
            await self.session.execute(
                delete(DecisionEvaluationRecord).where(
                    DecisionEvaluationRecord.decision_id.in_(
                        [row.decision_id for row in rows]
                    ),
                    DecisionEvaluationRecord.status == "PENDING",
                )
            )
            await self.session.flush()
        wf_ids = {row.workflow_id for row in rows if row.workflow_id}
        wf_venues: dict[Any, str] = {}
        if wf_ids:
            wf_rows = await self.session.execute(
                select(DailyWorkflowRun.id, DailyWorkflowRun.metadata_json).where(
                    DailyWorkflowRun.id.in_(wf_ids)
                )
            )
            for wf_id, meta in wf_rows.all():
                raw = (meta or {}).get("venue")
                if raw:
                    v = str(raw).upper()
                    if v in {"US", "AU"}:
                        wf_venues[wf_id] = v
            agent_rows = await self.session.execute(
                select(AgentRun.workflow_id, AgentReport.payload)
                .join(AgentReport, AgentReport.agent_run_id == AgentRun.id)
                .where(AgentRun.workflow_id.in_(wf_ids))
                .where(AgentRun.agent_name == "market_intelligence")
            )
            for wf_id, rep_payload in agent_rows.all():
                if wf_id in wf_venues:
                    continue
                book = (rep_payload or {}).get("trace", {}).get("book") or {}
                raw = book.get("venue") if isinstance(book, dict) else None
                if raw:
                    v = str(raw).upper()
                    if v in {"US", "AU"}:
                        wf_venues[wf_id] = v
        wl = list((await self.session.execute(select(WatchlistSymbol))).scalars().all())
        watchlist_hz = {r.symbol.upper(): str(r.horizon) for r in wl if r.symbol}
        resolver = DecisionPriceResolver(self.session)

        evaluations: list[dict[str, Any]] = []
        filled = 0
        pending = 0
        missing = 0

        for row in rows:
            payload = dict(row.payload or {})
            plans = list(payload.get("symbol_actions") or [])
            plan_syms = [
                str(p.get("symbol") or "").upper()
                for p in plans
                if isinstance(p, dict) and p.get("symbol")
            ]
            decision_venue = _decision_book_venue(
                payload=payload,
                workflow_id=row.workflow_id,
                wf_venues=wf_venues,
                symbol=plan_syms[0] if plan_syms else None,
                settings=self.settings,
            )
            benchmark = _benchmark_for_venue(decision_venue, self.settings)
            plan_horizons = [
                universe_horizon_for_plan(p if isinstance(p, dict) else {}, watchlist_horizon=watchlist_hz)
                for p in plans
                if isinstance(p, dict)
            ]
            primary = "unknown"
            if plan_horizons:
                non_unk = [h for h in plan_horizons if h != "unknown"]
                pick_from = non_unk or plan_horizons
                primary = max(set(pick_from), key=pick_from.count)

            decision_ts = row.decision_timestamp
            if decision_ts.tzinfo is None:
                decision_ts = decision_ts.replace(tzinfo=UTC)

            # Portfolio-level: prefer payload, else benchmark print as market proxy.
            explicit_px = payload.get("reference_price") or payload.get("decision_price")
            try:
                explicit_px_f = float(explicit_px) if explicit_px is not None else None
            except (TypeError, ValueError):
                explicit_px_f = None
            port_dec = await resolver.decision_price(
                benchmark,
                decision_ts,
                book=primary,
                explicit=explicit_px_f if explicit_px_f and explicit_px_f > 0 else None,
            )
            explicit_hp = payload.get("horizon_price")
            try:
                explicit_hp_f = float(explicit_hp) if explicit_hp is not None else None
            except (TypeError, ValueError):
                explicit_hp_f = None
            port_hz = await resolver.horizon_price(
                benchmark,
                decision_ts,
                book=primary,
                explicit=explicit_hp_f if explicit_hp_f and explicit_hp_f > 0 else None,
            )
            bench_ret = payload.get("benchmark_return")
            if bench_ret is None:
                bench_ret = await resolver.benchmark_return(benchmark, decision_ts, book=primary)

            price = float(port_dec.price or 0.0)
            horizon_price = port_hz.price
            if port_hz.source == "pending":
                pending += 1
            elif horizon_price is None:
                missing += 1
            else:
                filled += 1

            ev = evaluate_decision(
                decision_price=price,
                action=payload.get("portfolio_action", row.portfolio_action),
                horizon_price=horizon_price,
                benchmark_return=float(bench_ret) if bench_ret is not None else None,
            )
            eval_label = evaluation_horizon_label(primary)
            item = {
                "decision_id": str(row.decision_id),
                "action": row.portfolio_action,
                "symbol": benchmark if port_dec.price else None,
                "scope": "portfolio",
                "venue": decision_venue,
                "universe_horizon": primary,
                "horizons": sorted(set(plan_horizons)) if plan_horizons else [],
                "evaluation_horizon": eval_label,
                "decision_price": price or None,
                "horizon_price": horizon_price,
                "price_source": {"decision": port_dec.source, "horizon": port_hz.source},
                "evaluated_at": decision_ts.isoformat(),
                "metrics": _jsonable(ev),
            }
            evaluations.append(item)

            sym_prices = dict(payload.get("symbol_horizon_prices") or {})
            for plan in plans:
                if not isinstance(plan, dict):
                    continue
                sym = str(plan.get("symbol") or "").upper()
                if not sym:
                    continue
                hz = universe_horizon_for_plan(plan, watchlist_horizon=watchlist_hz)
                zone = plan.get("entry_zone") if isinstance(plan.get("entry_zone"), dict) else None
                explicit_plan_px = plan.get("decision_price")
                try:
                    explicit_plan_px_f = (
                        float(explicit_plan_px) if explicit_plan_px is not None else None
                    )
                except (TypeError, ValueError):
                    explicit_plan_px_f = None
                plan_dec = await resolver.decision_price(
                    sym,
                    decision_ts,
                    book=hz,
                    explicit=explicit_plan_px_f if explicit_plan_px_f and explicit_plan_px_f > 0 else None,
                    entry_zone=zone,
                )
                plan_hp_raw = plan.get("horizon_price")
                if plan_hp_raw is None:
                    plan_hp_raw = sym_prices.get(sym)
                try:
                    plan_hp_f = float(plan_hp_raw) if plan_hp_raw is not None else None
                except (TypeError, ValueError):
                    plan_hp_f = None
                plan_hz = await resolver.horizon_price(
                    sym,
                    decision_ts,
                    book=hz,
                    explicit=plan_hp_f if plan_hp_f and plan_hp_f > 0 else None,
                )
                plan_bench = bench_ret
                if plan_bench is None:
                    plan_bench = await resolver.benchmark_return(benchmark, decision_ts, book=hz)

                plan_price = float(plan_dec.price or 0.0)
                plan_hp = plan_hz.price
                if plan_hz.source == "pending":
                    pending += 1
                elif plan_hp is None:
                    missing += 1
                else:
                    filled += 1

                plan_ev = evaluate_decision(
                    decision_price=plan_price,
                    action=plan.get("action"),
                    horizon_price=plan_hp,
                    benchmark_return=float(plan_bench) if plan_bench is not None else None,
                )
                realized = None
                if plan_price > 0 and plan_hp is not None:
                    realized = (plan_hp - plan_price) / plan_price
                sym_label = evaluation_horizon_label(hz)
                sym_item = {
                    "decision_id": str(row.decision_id),
                    "action": plan.get("action"),
                    "symbol": sym,
                    "scope": "symbol",
                    "venue": _decision_book_venue(
                        payload=payload,
                        workflow_id=row.workflow_id,
                        wf_venues=wf_venues,
                        symbol=sym,
                        settings=self.settings,
                    ),
                    "universe_horizon": hz,
                    "evaluation_horizon": sym_label,
                    "decision_price": plan_price or None,
                    "horizon_price": plan_hp,
                    "price_source": {"decision": plan_dec.source, "horizon": plan_hz.source},
                    "horizon_end": (decision_ts + evaluation_horizon_delta(hz)).isoformat(),
                    "evaluated_at": decision_ts.isoformat(),
                    "metrics": _jsonable(plan_ev),
                }
                evaluations.append(sym_item)
                if persist:
                    status = (
                        "PENDING"
                        if plan_hz.source == "pending"
                        else ("AVAILABLE" if plan_hp is not None and plan_price > 0 else "UNAVAILABLE")
                    )
                    self.session.add(
                        DecisionEvaluationRecord(
                            decision_id=row.decision_id,
                            decision_type="cio_symbol",
                            action=str(plan.get("action") or row.portfolio_action),
                            symbol=sym,
                            decision_price=plan_price,
                            evaluation_horizon=sym_label,
                            price_at_horizon=plan_hp,
                            return_after_decision=realized,
                            benchmark_return_after_decision=(
                                float(plan_bench) if plan_bench is not None else None
                            ),
                            excess_return=(
                                (realized - float(plan_bench))
                                if realized is not None and plan_bench is not None
                                else None
                            ),
                            evaluated_at=datetime.now(UTC),
                            payload=sym_item,
                            status=status,
                        )
                    )

            if persist:
                port_realized = None
                if price > 0 and horizon_price is not None:
                    port_realized = (horizon_price - price) / price
                port_status = (
                    "PENDING"
                    if port_hz.source == "pending"
                    else ("AVAILABLE" if horizon_price is not None and price > 0 else "UNAVAILABLE")
                )
                self.session.add(
                    DecisionEvaluationRecord(
                        decision_id=row.decision_id,
                        decision_type="cio",
                        action=row.portfolio_action,
                        symbol=benchmark if port_dec.price else None,
                        decision_price=price,
                        evaluation_horizon=eval_label,
                        price_at_horizon=horizon_price,
                        return_after_decision=port_realized,
                        benchmark_return_after_decision=(
                            float(bench_ret) if bench_ret is not None else None
                        ),
                        excess_return=(
                            (port_realized - float(bench_ret))
                            if port_realized is not None and bench_ret is not None
                            else None
                        ),
                        evaluated_at=datetime.now(UTC),
                        payload=item,
                        status=port_status,
                    )
                )
            if persist:
                await self.session.flush()
        evaluations.sort(key=lambda e: str(e.get("evaluated_at") or ""), reverse=True)
        summary = summarize_decision_evaluations(evaluations)
        return {
            "count": len(evaluations),
            "decisions_processed": len(rows),
            "remaining_decisions": remaining_after,
            "evaluations": evaluations,
            "summary": summary,
            "price_resolution": {
                "filled": filled,
                "pending": pending,
                "missing": missing,
                "benchmark": benchmark,
            },
            "horizon_note": (
                "horizon_price from market_snapshots within book max_holding window "
                "(scalp 4h / day 1session / short 10d / medium 60d); "
                "pending until horizon elapses; no look-ahead past horizon_end"
            ),
        }

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
                payload = dict(row.payload or {})
                actual = _actual_return_from_payload(payload)
                if payload.get("direction_correct") is None and actual is not None:
                    view = row.directional_view
                    if view in {None, "ABSTAIN", "NEUTRAL"}:
                        payload["direction_correct"] = None
                    elif view == "BULLISH":
                        payload["direction_correct"] = actual > 0
                    elif view == "BEARISH":
                        payload["direction_correct"] = actual < 0
                if payload.get("actual_return") is None and actual is not None:
                    payload["actual_return"] = actual
                if "universe_horizon" not in payload:
                    payload["universe_horizon"] = None
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


def _actual_return_from_payload(payload: dict[str, Any]) -> float | None:
    """Prefer explicit actual_return; fall back to signed PnL as directional proxy."""
    if payload.get("actual_return") is not None:
        try:
            return float(payload["actual_return"])
        except (TypeError, ValueError):
            pass
    if payload.get("pnl") is not None:
        try:
            return float(payload["pnl"])
        except (TypeError, ValueError):
            return None
    return None
