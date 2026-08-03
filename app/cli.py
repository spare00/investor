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
    else:
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
