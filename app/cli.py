"""CLI entrypoints for analysis and daily workflow ops (Phase 2–3)."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from app.agents.pipeline import AgentPipeline
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.market.calendar import MarketCalendarService
from app.schemas.risk_manager import PortfolioStateInput
from app.services.collection import DataCollectionService
from app.services.llm import FakeLLMProvider
from app.workflow.daily import DailyWorkflowService
from app.workflow.recovery import RecoveryService


async def _run_analysis(*, fixture: Path | None, use_fake_llm: bool) -> dict:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        if fixture and fixture.exists():
            _ = json.loads(fixture.read_text(encoding="utf-8"))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run-analysis", help="Run bottom-up analysis without broker orders")
    run.add_argument("--fixture", type=Path, default=None)
    run.add_argument("--fake-llm", action="store_true", help="Force FakeLLMProvider (fallbacks)")

    sub.add_parser("market-status", help="Show US market session status")

    cal = sub.add_parser("calendar", help="Show session info for a date")
    cal.add_argument("--date", type=date.fromisoformat, required=True)

    daily = sub.add_parser("daily-workflow", help="Daily workflow operations")
    daily_sub = daily.add_subparsers(dest="daily_cmd", required=True)
    for name in ("prepare", "run-analysis", "revalidate", "status"):
        p = daily_sub.add_parser(name)
        p.add_argument("--date", dest="session_date", default=None)
        if name == "run-analysis":
            p.add_argument("--fake-llm", action="store_true")

    sched = sub.add_parser("scheduler", help="Scheduler inspection")
    sched_sub = sched.add_subparsers(dest="sched_cmd", required=True)
    sched_sub.add_parser("list")

    recovery = sub.add_parser("recovery", help="Run recovery service")
    recovery_sub = recovery.add_subparsers(dest="recovery_cmd", required=True)
    recovery_sub.add_parser("run")

    args = parser.parse_args(argv)

    if args.cmd == "run-analysis":
        result = asyncio.run(_run_analysis(fixture=args.fixture, use_fake_llm=args.fake_llm))
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
    else:
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
