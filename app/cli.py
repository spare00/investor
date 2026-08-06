"""CLI entrypoints for analysis, daily workflow, and data layer (Phase 2–4)."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.agents.pipeline import AgentPipeline
from app.context_builders.builders import MarketIntelligenceContextBuilder
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.ingestion.pipeline import DataCollectionPipeline
from app.market.calendar import MarketCalendarService
from app.providers.registry import list_providers
from app.schemas.risk_manager import PortfolioStateInput
from app.services.collection import DataCollectionService
from app.services.llm import FakeLLMProvider
from app.workflow.daily import DailyWorkflowService
from app.workflow.recovery import RecoveryService


async def _run_analysis(*, fixture: Path | None, use_fake_llm: bool, real_data: bool) -> dict:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        if fixture and fixture.exists():
            _ = json.loads(fixture.read_text(encoding="utf-8"))
        if real_data:
            data = await DataCollectionPipeline(
                session, settings=settings, fixture_mode=not settings.enable_external_data
            ).collect("PREMARKET")
            collection = data.legacy_bundle
            assert collection is not None
        else:
            collection = await DataCollectionService(session, persist=False).collect_premarket(
                workflow_id=uuid4()
            )
        llm = FakeLLMProvider({}) if use_fake_llm else None
        pipeline = (
            AgentPipeline(settings=settings, llm=llm)
            if llm is not None
            else AgentPipeline(settings=settings)
        )
        portfolio = PortfolioStateInput(
            as_of=datetime.now(UTC),
            equity=settings.starting_cash,
            cash=settings.starting_cash,
            cash_pct=100.0,
            gross_exposure_pct=0.0,
        )
        analysis = await pipeline.run_from_collection(
            collection,
            portfolio=portfolio,
            proposed_trades=[],
        )
        await session.commit()
        return {
            "workflow_id": str(analysis.workflow_id),
            "broker_orders_submitted": False,
            "cio_action": analysis.cio.portfolio_action.value,
            "regime": analysis.macro.market_regime.value,
            "risk": analysis.risk.overall_verdict.value,
            "completed_at": analysis.completed_at.isoformat(),
        }


async def _market_status() -> dict:
    return MarketCalendarService(get_settings()).get_market_status().to_dict()


async def _calendar(day: date) -> dict:
    return MarketCalendarService(get_settings()).get_session(day).to_dict()


async def _daily(cmd: str, *, session_date: str | None, fake_llm: bool) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        svc = DailyWorkflowService(session, settings=get_settings())
        if cmd == "prepare":
            result = await svc.prepare(session_date=session_date)
        elif cmd == "run-analysis":
            result = await svc.run_analysis(session_date=session_date, fake_llm=fake_llm)
        elif cmd == "revalidate":
            result = await svc.revalidate(session_date=session_date, fake_llm=fake_llm)
        elif cmd == "status":
            run = await svc.get_current(session_date)
            result = {"run": None if run is None else svc._run_dict(run)}
        else:
            raise SystemExit(f"unknown daily-workflow command: {cmd}")
        await session.commit()
        return result


async def _scheduler_list() -> dict:
    from app.core.scheduler import upcoming_jobs

    factory = get_session_factory()
    async with factory() as session:
        planned = await DailyWorkflowService(session).planned_jobs()
        return {
            "enable_scheduler": get_settings().enable_scheduler,
            "runtime_jobs": upcoming_jobs(),
            "planned_jobs": planned,
        }


async def _recovery() -> dict:
    factory = get_session_factory()
    async with factory() as session:
        result = await RecoveryService(session).run()
        await session.commit()
        return result


async def _collect(kind: str, *, fixture: bool, symbols: list[str] | None, since_h: int | None) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        mapping = {
            "premarket": "PREMARKET",
            "intraday": "INTRADAY",
            "news": "ON_DEMAND",
            "sec": "ON_DEMAND",
            "macro": "ON_DEMAND",
            "revalidation": "PREOPEN_REVALIDATION",
            "postmarket": "POSTMARKET",
        }
        result = await DataCollectionPipeline(
            session, fixture_mode=fixture
        ).collect(mapping.get(kind, "ON_DEMAND"), symbols=symbols)
        await session.commit()
        return result.to_dict()


async def _build_context(kind: str) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        data = await DataCollectionPipeline(session, fixture_mode=True).collect("ON_DEMAND")
        if kind == "market-intelligence":
            return MarketIntelligenceContextBuilder().build(
                news=data.news, filings=data.filings, conflicts=data.conflicts
            )
        return data.contexts.get(kind.replace("-", "_"), data.contexts)


async def _broker_cmd(action: str) -> dict:
    from app.brokers.factory import get_broker

    settings = get_settings()
    broker = get_broker(settings)
    if action == "status":
        health = await broker.health_check() if hasattr(broker, "health_check") else None
        return {
            "provider": settings.broker_provider,
            "environment": settings.broker_environment,
            "enable_broker_connection": settings.enable_broker_connection,
            "enable_broker_orders": settings.enable_broker_orders,
            "health": None if health is None else health.model_dump(mode="json"),
        }
    if action == "account":
        if hasattr(broker, "get_account_canonical"):
            return (await broker.get_account_canonical()).model_dump(mode="json")
        return dict(await broker.get_account())
    if action == "positions":
        if hasattr(broker, "get_positions_canonical"):
            return {"positions": [p.model_dump(mode="json") for p in await broker.get_positions_canonical()]}
        return {"positions": await broker.get_positions()}
    if action == "orders":
        orders = await broker.get_open_orders() if hasattr(broker, "get_open_orders") else []
        return {
            "orders": [
                {
                    "broker_order_id": o.broker_order_id,
                    "status": o.status.value,
                    "filled_qty": o.filled_qty,
                }
                for o in orders
            ]
        }
    raise SystemExit(f"unknown broker action: {action}")


async def _execution_cmd(action: str, **kwargs: object) -> dict:
    from uuid import UUID

    from app.execution.reconciliation import ReconciliationService
    from app.execution.service import ExecutionService
    from app.risk import PortfolioRiskView
    from app.schemas.cio import CIODecision

    factory = get_session_factory()
    async with factory() as session:
        svc = ExecutionService(session)
        if action == "intents_list":
            rows = await svc.list_intents()
            result = {
                "intents": [
                    {
                        "intent_id": str(i.id),
                        "symbol": i.symbol,
                        "status": i.status,
                        "side": i.side,
                        "quantity": i.quantity,
                    }
                    for i in rows
                ]
            }
        elif action == "build_intents":
            decision_id = UUID(str(kwargs["decision_id"]))
            from sqlalchemy import select

            from app.models import CIODecisionRecord

            row = (
                await session.execute(
                    select(CIODecisionRecord).where(CIODecisionRecord.decision_id == decision_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = await session.get(CIODecisionRecord, decision_id)
            if row is None:
                raise SystemExit("decision_not_found")
            decision = CIODecision.model_validate(row.payload)
            settings = get_settings()
            portfolio = PortfolioRiskView(
                equity=settings.starting_cash,
                cash=settings.starting_cash,
                cash_pct=100.0,
                gross_exposure_pct=0.0,
            )
            intents = await svc.build_intents_from_decision(
                decision, portfolio=portfolio, latest_prices={}
            )
            result = {"intents": [str(i.id) for i in intents]}
        elif action == "validate":
            result = (
                await svc.validate_intent(
                    UUID(str(kwargs["intent_id"])),
                    equity=get_settings().starting_cash,
                    cash=get_settings().starting_cash,
                    buying_power=get_settings().starting_cash,
                    gross_exposure=0.0,
                    position_qty=0.0,
                )
            ).to_dict()
        elif action == "approve":
            intent = await svc.approve_intent(UUID(str(kwargs["intent_id"])))
            result = {"intent_id": str(intent.id), "status": intent.status}
        elif action == "reject":
            intent = await svc.reject_intent(UUID(str(kwargs["intent_id"])), reason=str(kwargs.get("reason") or ""))
            result = {"intent_id": str(intent.id), "status": intent.status}
        elif action == "submit":
            order = await svc.submit_intent(UUID(str(kwargs["intent_id"])))
            result = {
                "submitted": order is not None,
                "order_id": None if order is None else str(order.id),
                "status": None if order is None else order.status,
            }
        elif action == "reconcile":
            result = await ReconciliationService(session).run("ON_DEMAND")
        elif action == "cancel_all":
            from app.brokers.factory import get_broker

            n = await get_broker(get_settings()).cancel_all_orders()  # type: ignore[attr-defined]
            result = {"canceled": n}
        else:
            raise SystemExit(f"unknown execution action: {action}")
        await session.commit()
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run-analysis", help="Run bottom-up analysis without broker orders")
    run.add_argument("--fixture", type=Path, default=None)
    run.add_argument("--fake-llm", action="store_true")
    run.add_argument("--real-data", action="store_true")
    run.add_argument("--no-broker", action="store_true", default=True)

    sub.add_parser("market-status")
    cal = sub.add_parser("calendar")
    cal.add_argument("--date", type=date.fromisoformat, required=True)

    daily = sub.add_parser("daily-workflow")
    daily_sub = daily.add_subparsers(dest="daily_cmd", required=True)
    for name in ("prepare", "run-analysis", "revalidate", "status"):
        p = daily_sub.add_parser(name)
        p.add_argument("--date", dest="session_date", default=None)
        if name == "run-analysis":
            p.add_argument("--fake-llm", action="store_true")

    sched = sub.add_parser("scheduler")
    sched.add_subparsers(dest="sched_cmd", required=True).add_parser("list")

    recovery = sub.add_parser("recovery")
    recovery.add_subparsers(dest="recovery_cmd", required=True).add_parser("run")

    providers = sub.add_parser("providers")
    psub = providers.add_subparsers(dest="providers_cmd", required=True)
    psub.add_parser("list")
    psub.add_parser("health")
    psub.add_parser("reliability")

    collect = sub.add_parser("collect")
    csub = collect.add_subparsers(dest="collect_cmd", required=True)
    for name in ("premarket", "intraday", "news", "sec", "macro"):
        cp = csub.add_parser(name)
        cp.add_argument("--fixture", action="store_true", default=True)
        cp.add_argument("--symbols", default=None)
        if name == "news":
            cp.add_argument("--since", default=None)

    dq = sub.add_parser("data-quality")
    dq.add_subparsers(dest="dq_cmd", required=True).add_parser("report")

    dc = sub.add_parser("data-conflicts")
    dc.add_subparsers(dest="dc_cmd", required=True).add_parser("list")

    bc = sub.add_parser("build-context")
    bc.add_argument("context_kind", choices=["market-intelligence", "macro", "quant"])

    wf = sub.add_parser("workflow")
    wf_sub = wf.add_subparsers(dest="wf_cmd", required=True)
    ra = wf_sub.add_parser("run-analysis")
    ra.add_argument("--real-data", action="store_true")
    ra.add_argument("--fake-llm", action="store_true")
    ra.add_argument("--no-broker", action="store_true", default=True)

    broker = sub.add_parser("broker")
    bsub = broker.add_subparsers(dest="broker_cmd", required=True)
    for name in ("status", "account", "positions", "orders"):
        bsub.add_parser(name)

    execution = sub.add_parser("execution")
    esub = execution.add_subparsers(dest="execution_cmd", required=True)
    intents = esub.add_parser("intents")
    intents_sub = intents.add_subparsers(dest="intents_cmd", required=True)
    intents_sub.add_parser("list")
    build = esub.add_parser("build-intents")
    build.add_argument("--decision-id", required=True)
    validate = esub.add_parser("validate")
    validate.add_argument("--intent-id", required=True)
    approve = esub.add_parser("approve")
    approve.add_argument("--intent-id", required=True)
    reject = esub.add_parser("reject")
    reject.add_argument("--intent-id", required=True)
    reject.add_argument("--reason", default="")
    submit = esub.add_parser("submit")
    submit.add_argument("--intent-id", required=True)
    esub.add_parser("reconcile")
    esub.add_parser("cancel-all")

    intraday = sub.add_parser("intraday")
    isub = intraday.add_subparsers(dest="intraday_cmd", required=True)
    isub.add_parser("status")
    ievents = isub.add_parser("events")
    ievents_sub = ievents.add_subparsers(dest="events_cmd", required=True)
    ievents_sub.add_parser("list")
    isub.add_parser("evaluate")
    isub.add_parser("recovery")

    positions = sub.add_parser("positions")
    psub = positions.add_subparsers(dest="positions_cmd", required=True)
    psub.add_parser("monitor")
    preview = psub.add_parser("review")
    preview.add_argument("--symbol", required=True)
    pclose = psub.add_parser("close")
    pclose.add_argument("--symbol", required=True)

    closing = sub.add_parser("closing")
    closing.add_subparsers(dest="closing_cmd", required=True).add_parser("run")

    overnight = sub.add_parser("overnight")
    overnight.add_subparsers(dest="overnight_cmd", required=True).add_parser("review")

    postmarket = sub.add_parser("postmarket")
    postmarket.add_subparsers(dest="postmarket_cmd", required=True).add_parser("settle")

    posttrade = sub.add_parser("posttrade")
    ptsub = posttrade.add_subparsers(dest="posttrade_cmd", required=True)
    ptrev = ptsub.add_parser("review")
    ptrev.add_argument("--position-id", required=True)
    ptrev.add_argument("--symbol", default="UNKNOWN")

    performance = sub.add_parser("performance")
    perf_sub = performance.add_subparsers(dest="perf_cmd", required=True)
    for name in ("portfolio", "risk", "drawdowns", "trades", "agents", "recalculate"):
        perf_sub.add_parser(name)

    operations = sub.add_parser("operations")
    operations.add_subparsers(dest="ops_cmd", required=True).add_parser("metrics")

    alerts = sub.add_parser("alerts")
    alert_sub = alerts.add_subparsers(dest="alert_cmd", required=True)
    alert_sub.add_parser("list")
    alert_ack = alert_sub.add_parser("acknowledge")
    alert_ack.add_argument("--id", required=True)

    simulation = sub.add_parser("simulation")
    sim_sub = simulation.add_subparsers(dest="sim_cmd", required=True)
    sim_run = sim_sub.add_parser("run")
    sim_run.add_argument("--scenario", default="bull-market")
    sim_run.add_argument("--days", type=int, default=5)
    sim_report = sim_sub.add_parser("report")
    sim_report.add_argument("--id", required=True)

    readiness = sub.add_parser("readiness")
    readiness.add_subparsers(dest="readiness_cmd", required=True).add_parser("evaluate")

    backup = sub.add_parser("backup")
    backup_sub = backup.add_subparsers(dest="backup_cmd", required=True)
    backup_sub.add_parser("create")
    backup_verify = backup_sub.add_parser("verify")
    backup_verify.add_argument("--path", required=True)

    security = sub.add_parser("security", help="Security audit checks (Phase 7)")
    security.add_subparsers(dest="security_cmd").add_parser("audit")

    universe = sub.add_parser("universe", help="AI watchlist / focus set")
    universe_sub = universe.add_subparsers(dest="universe_cmd", required=True)
    universe_sub.add_parser("show")
    universe_sub.add_parser("horizons")
    ur = universe_sub.add_parser("refresh")
    ur.add_argument(
        "--force",
        action="store_true",
        help="Bypass weekly LLM min-interval (burns budget)",
    )

    args = parser.parse_args(argv)

    if args.cmd == "run-analysis":
        result = asyncio.run(
            _run_analysis(
                fixture=args.fixture, use_fake_llm=args.fake_llm, real_data=args.real_data
            )
        )
    elif args.cmd == "workflow" and args.wf_cmd == "run-analysis":
        result = asyncio.run(
            _run_analysis(fixture=None, use_fake_llm=args.fake_llm, real_data=args.real_data)
        )
    elif args.cmd == "market-status":
        result = asyncio.run(_market_status())
    elif args.cmd == "calendar":
        result = asyncio.run(_calendar(args.date))
    elif args.cmd == "daily-workflow":
        fake = getattr(args, "fake_llm", False)
        result = asyncio.run(
            _daily(args.daily_cmd, session_date=getattr(args, "session_date", None), fake_llm=fake)
        )
    elif args.cmd == "scheduler" and args.sched_cmd == "list":
        result = asyncio.run(_scheduler_list())
    elif args.cmd == "recovery" and args.recovery_cmd == "run":
        result = asyncio.run(_recovery())
    elif args.cmd == "providers" and args.providers_cmd == "list":
        result = {"providers": list_providers(get_settings())}
    elif args.cmd == "providers" and args.providers_cmd == "health":
        from app.providers.base import get_breaker

        settings = get_settings()
        result = {
            "health": [
                {"name": n, "allow": get_breaker(n, settings).allow()}
                for n in ("fixture", "alpaca", "sec_edgar")
            ]
        }
    elif args.cmd == "providers" and args.providers_cmd == "reliability":
        result = asyncio.run(_providers_reliability())
    elif args.cmd == "collect":
        syms = (
            [s.strip().upper() for s in args.symbols.split(",")]
            if getattr(args, "symbols", None)
            else None
        )
        result = asyncio.run(
            _collect(args.collect_cmd, fixture=getattr(args, "fixture", True), symbols=syms, since_h=None)
        )
    elif args.cmd == "data-quality" and args.dq_cmd == "report":
        result = asyncio.run(_collect("premarket", fixture=True, symbols=None, since_h=None))
        result = {"quality_summary": result.get("quality_summary"), "fail_closed": result.get("fail_closed")}
    elif args.cmd == "data-conflicts" and args.dc_cmd == "list":
        raw = asyncio.run(_collect("premarket", fixture=True, symbols=None, since_h=None))
        result = {"conflicts": raw.get("provider_metas")}  # lightweight; full conflicts in API cache
    elif args.cmd == "build-context":
        result = asyncio.run(_build_context(args.context_kind))
    elif args.cmd == "broker":
        result = asyncio.run(_broker_cmd(args.broker_cmd))
    elif args.cmd == "execution":
        if args.execution_cmd == "intents" and args.intents_cmd == "list":
            result = asyncio.run(_execution_cmd("intents_list"))
        elif args.execution_cmd == "build-intents":
            result = asyncio.run(_execution_cmd("build_intents", decision_id=args.decision_id))
        elif args.execution_cmd == "validate":
            result = asyncio.run(_execution_cmd("validate", intent_id=args.intent_id))
        elif args.execution_cmd == "approve":
            result = asyncio.run(_execution_cmd("approve", intent_id=args.intent_id))
        elif args.execution_cmd == "reject":
            result = asyncio.run(_execution_cmd("reject", intent_id=args.intent_id, reason=args.reason))
        elif args.execution_cmd == "submit":
            result = asyncio.run(_execution_cmd("submit", intent_id=args.intent_id))
        elif args.execution_cmd == "reconcile":
            result = asyncio.run(_execution_cmd("reconcile"))
        elif args.execution_cmd == "cancel-all":
            result = asyncio.run(_execution_cmd("cancel_all"))
        else:
            return 1
    elif args.cmd == "intraday":
        result = asyncio.run(_intraday_cmd(args))
    elif args.cmd == "positions":
        result = asyncio.run(_positions_cmd(args))
    elif args.cmd == "closing" and args.closing_cmd == "run":
        result = asyncio.run(_closing_run())
    elif args.cmd == "overnight" and args.overnight_cmd == "review":
        result = asyncio.run(_overnight_review())
    elif args.cmd == "postmarket" and args.postmarket_cmd == "settle":
        result = asyncio.run(_postmarket_settle())
    elif args.cmd == "posttrade" and args.posttrade_cmd == "review":
        result = asyncio.run(_posttrade_review(args.position_id, args.symbol))
    elif args.cmd == "performance":
        result = asyncio.run(_performance_cmd(args.perf_cmd))
    elif args.cmd == "operations" and args.ops_cmd == "metrics":
        result = asyncio.run(_operations_metrics())
    elif args.cmd == "alerts":
        result = asyncio.run(_alerts_cmd(args))
    elif args.cmd == "simulation":
        result = asyncio.run(_simulation_cmd(args))
    elif args.cmd == "readiness" and args.readiness_cmd == "evaluate":
        result = asyncio.run(_readiness_evaluate())
    elif args.cmd == "backup":
        result = asyncio.run(_backup_cmd(args))
    elif args.cmd == "security":
        result = _security_audit()
    elif args.cmd == "universe":
        result = asyncio.run(
            _universe_cmd(args.universe_cmd, force=bool(getattr(args, "force", False)))
        )
    else:
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


async def _intraday_cmd(args: argparse.Namespace) -> dict:
    from app.intraday.service import IntradayService

    factory = get_session_factory()
    async with factory() as session:
        svc = IntradayService(session)
        if args.intraday_cmd == "status":
            result = svc.status()
        elif args.intraday_cmd == "events" and args.events_cmd == "list":
            rows = await svc.bus.list_events()
            result = {"events": [{"id": str(e.id), "type": e.event_type, "status": e.status} for e in rows]}
        elif args.intraday_cmd == "evaluate":
            result = await svc.agents.evaluate(fake_llm=True)
        elif args.intraday_cmd == "recovery":
            result = await svc.recovery.run()
        else:
            raise SystemExit("unknown intraday command")
        await session.commit()
        return result


async def _positions_cmd(args: argparse.Namespace) -> dict:
    from app.intraday.service import IntradayService
    from app.models import PositionLifecycle
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        svc = IntradayService(session)
        if args.positions_cmd == "monitor":
            result = {"results": await svc.monitor_all()}
        elif args.positions_cmd == "review":
            row = (
                await session.execute(
                    select(PositionLifecycle).where(PositionLifecycle.symbol == args.symbol.upper()).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                raise SystemExit("position_not_found")
            mon = await svc.monitor.evaluate(row, current_price=row.current_price, equity=get_settings().starting_cash)
            result = {"verdict": mon.verdict, "reasons": mon.reasons}
        elif args.positions_cmd == "close":
            row = (
                await session.execute(
                    select(PositionLifecycle).where(PositionLifecycle.symbol == args.symbol.upper()).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                raise SystemExit("position_not_found")
            result = await svc.close_position(row.id)
        else:
            raise SystemExit("unknown positions command")
        await session.commit()
        return result


async def _closing_run() -> dict:
    from app.intraday.service import IntradayService

    factory = get_session_factory()
    async with factory() as session:
        result = await IntradayService(session).closing.run_closing()
        await session.commit()
        return result


async def _overnight_review() -> dict:
    from app.intraday.service import IntradayService

    factory = get_session_factory()
    async with factory() as session:
        result = await IntradayService(session).closing.overnight_review()
        await session.commit()
        return result


async def _postmarket_settle() -> dict:
    from app.intraday.service import IntradayService

    factory = get_session_factory()
    async with factory() as session:
        result = await IntradayService(session).settlement.settle()
        await session.commit()
        return result


async def _posttrade_review(position_id: str, symbol: str) -> dict:
    from uuid import UUID

    from app.intraday.service import IntradayService

    factory = get_session_factory()
    async with factory() as session:
        result = await IntradayService(session).posttrade.create_review(
            position_lifecycle_id=UUID(position_id),
            symbol=symbol,
            outcome="closed",
            exit_reason="cli",
        )
        await session.commit()
        return result


async def _providers_reliability() -> dict:
    from app.performance.service import PerformanceService

    factory = get_session_factory()
    async with factory() as session:
        stats = {"providers": list_providers(get_settings())}
        return PerformanceService(session).providers(stats)


async def _performance_cmd(cmd: str) -> dict:
    from datetime import UTC, datetime, timedelta

    from app.performance.service import PerformanceService

    end = datetime.now(UTC)
    start = end - timedelta(days=90)
    factory = get_session_factory()
    async with factory() as session:
        svc = PerformanceService(session, settings=get_settings())
        if cmd == "portfolio":
            result = await svc.portfolio_summary(start, end)
        elif cmd == "risk":
            result = await svc.risk_summary(start, end)
        elif cmd == "drawdowns":
            result = await svc.drawdowns(start, end)
        elif cmd == "trades":
            result = await svc.trade_metrics(start, end)
        elif cmd == "agents":
            result = await svc.agents(start, end)
        elif cmd == "recalculate":
            result = await svc.recalculate(start, end)
        else:
            raise SystemExit(f"unknown performance command: {cmd}")
        return result


async def _operations_metrics() -> dict:
    from app.performance.service import PerformanceService

    factory = get_session_factory()
    async with factory() as session:
        counters = {"window_seconds": 86400}
        return {"kpis": PerformanceService(session).operational(counters)}


async def _alerts_cmd(args: argparse.Namespace) -> dict:
    from uuid import UUID

    from app.alerts.service import AlertService

    factory = get_session_factory()
    async with factory() as session:
        svc = AlertService(session, settings=get_settings())
        if args.alert_cmd == "list":
            alerts = await svc.list_alerts()
            return {
                "alerts": [
                    {"id": str(a.id), "code": a.code, "severity": a.severity.value, "status": a.status.value}
                    for a in alerts
                ]
            }
        if args.alert_cmd == "acknowledge":
            out = await svc.acknowledge(UUID(args.id))
            await session.commit()
            return {"emitted": out.emitted, "reason": out.reason, "alert_id": str(out.alert_id)}
        raise SystemExit("unknown alerts command")


async def _simulation_cmd(args: argparse.Namespace) -> dict:
    from uuid import UUID

    from app.simulation.runner import MultiDaySimulationRunner

    factory = get_session_factory()
    async with factory() as session:
        if args.sim_cmd == "run":
            try:
                summary = await MultiDaySimulationRunner(session, settings=get_settings()).run(
                    args.scenario, days=args.days
                )
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                return summary.to_dict() if hasattr(summary, "to_dict") else summary
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                # Offline-friendly: run without DB persistence
                summary = await MultiDaySimulationRunner(None, settings=get_settings()).run(
                    args.scenario, days=args.days
                )
                payload = summary.to_dict() if hasattr(summary, "to_dict") else summary
                if isinstance(payload, dict):
                    payload["persist_warning"] = str(exc)[:200]
                return payload
        if args.sim_cmd == "report":
            from app.models import SimulationRunRecord

            row = await session.get(SimulationRunRecord, UUID(args.id))
            if row is None:
                raise SystemExit("simulation_not_found")
            return {"id": str(row.id), "scenario": row.scenario, "payload": row.payload, "status": row.status}
        raise SystemExit("unknown simulation command")


async def _readiness_evaluate() -> dict:
    from datetime import UTC, datetime

    from app.ops.readiness import GateEvaluator

    evaluator = GateEvaluator(get_settings())
    result = evaluator.evaluate(evaluator.default_gate())
    factory = get_session_factory()
    async with factory() as session:
        try:
            from app.models import ReadinessEvaluationRecord

            row = ReadinessEvaluationRecord(
                gate=result["current_gate"],
                result=result,
                evaluated_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            return {"evaluation_id": str(row.id), **result}
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            return {**result, "persisted": False, "persist_warning": str(exc)[:200]}


async def _backup_cmd(args: argparse.Namespace) -> dict:
    from app.ops.backup import BackupService

    factory = get_session_factory()
    async with factory() as session:
        svc = BackupService(session=session)
        if args.backup_cmd == "create":
            created = await svc.create(as_zip=True)
            return {
                "backup_id": created.backup_id,
                "path": created.path,
                "file_count": created.file_count,
            }
        if args.backup_cmd == "verify":
            verified = svc.verify(args.path)
            return {"valid": verified.valid, "errors": verified.errors, "backup_id": verified.backup_id}
        raise SystemExit("unknown backup command")


async def _universe_cmd(cmd: str, *, force: bool = False) -> dict:
    from sqlalchemy import select

    from app.models import Position
    from app.universe.horizons import all_horizon_summaries
    from app.universe.service import UniverseService

    if cmd == "horizons":
        return {"horizons": all_horizon_summaries()}

    factory = get_session_factory()
    async with factory() as session:
        svc = UniverseService(session, settings=get_settings())
        if cmd == "show":
            snap = await svc.snapshot()
            await session.commit()
            return snap
        if cmd == "refresh":
            holdings = [
                p.symbol for p in (await session.execute(select(Position))).scalars().all()
            ]
            result = await svc.refresh(holdings=holdings, force=force)
            await session.commit()
            return result
        raise SystemExit(f"unknown universe command: {cmd}")


def _security_audit() -> dict:
    from pathlib import Path

    doc = Path(__file__).resolve().parents[1] / "docs" / "security_audit_phase7.md"
    settings = get_settings()
    checks = [
        {
            "name": "live_trading_allowed",
            "passed": not settings.is_live_trading_allowed(),
            "detail": "LIVE must remain blocked",
        },
        {
            "name": "enable_live_trading_flag",
            "passed": not settings.enable_live_trading,
            "detail": "ENABLE_LIVE_TRADING must be false",
        },
        {
            "name": "dashboard_read_only",
            "passed": settings.dashboard_read_only,
            "detail": "Dashboard must stay read-only",
        },
    ]
    return {
        "audit_doc": str(doc),
        "audit_doc_exists": doc.exists(),
        "checks": checks,
        "all_passed": all(c["passed"] for c in checks),
    }


if __name__ == "__main__":
    raise SystemExit(main())
