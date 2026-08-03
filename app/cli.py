"""CLI entrypoints for offline analysis (Phase 2)."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.agents.pipeline import AgentPipeline
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.schemas.risk_manager import PortfolioStateInput
from app.services.collection import DataCollectionService
from app.services.llm import FakeLLMProvider


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run-analysis", help="Run bottom-up analysis without broker orders")
    run.add_argument("--fixture", type=Path, default=None)
    run.add_argument("--fake-llm", action="store_true", help="Force FakeLLMProvider (fallbacks)")
    args = parser.parse_args(argv)
    if args.cmd == "run-analysis":
        result = asyncio.run(_run_analysis(fixture=args.fixture, use_fake_llm=args.fake_llm))
        print(json.dumps(result, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
