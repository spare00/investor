"""A method trial keeps the entry technique and the exit facts apart from the score."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import Settings, clear_settings_cache
from app.core.database import Base
from app.intraday.monitor import PositionMonitor
from app.models import (
    Execution,
    MethodTrial,
    Order,
    PositionLifecycle,
    PositionSnapshotRecord,
    WatchlistSymbol,
)
from app.performance.method_trial import merge_exit, price_path, trial_feedback
from app.performance.method_trial_store import complete_method_trial


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def test_price_path_names_which_level_printed_first() -> None:
    start = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    assert (
        price_path(
            [
                (start, 100),
                (start + timedelta(minutes=1), 101),
                (start + timedelta(minutes=2), 98),
            ],
            stop=99,
            target=100.8,
        )
        == "target_first"
    )
    assert (
        price_path(
            [
                (start, 100),
                (start + timedelta(minutes=1), 98),
                (start + timedelta(minutes=2), 101),
            ],
            stop=99,
            target=100.8,
        )
        == "stop_first"
    )
    assert (
        price_path(
            [(start, 100), (start + timedelta(minutes=1), 98)],
            stop=99,
            target=100.8,
        )
        == "stop_only"
    )
    assert price_path([(start, 100.2)], stop=99, target=100.8) == "neither"
    assert price_path([(start, 100)], stop=None, target=None) == "unobserved"
    assert price_path([], stop=99, target=100.8) == "unobserved"
    assert price_path([(start, 99.5)], stop=100, target=99) == "ambiguous"


def test_known_exit_facts_are_not_replaced() -> None:
    first = merge_exit(
        {
            "status": "open",
            "entry_captured_at_open": True,
            "counts_for_method": False,
            "fees_known": False,
        },
        {
            "gross_pnl": -1.5,
            "execution_verdict": "unknown",
            "path": "unobserved",
            "fees_known": False,
        },
    )
    filled = merge_exit(
        first,
        {
            "gross_pnl": 9,
            "execution_verdict": "faithful",
            "strategy_verdict": "loss",
            "cause": "faithful_close",
            "path": "stop_first",
            "fees_known": True,
            "fee": 0.2,
        },
    )
    assert filled["gross_pnl"] == -1.5
    assert filled["execution_verdict"] == "faithful"
    assert filled["path"] == "stop_first"
    assert filled["fees_known"] is True
    assert filled["counts_for_method"] is True
    locked = merge_exit(
        filled, {"path": "target_first", "gross_pnl": 9, "execution_verdict": "broken"}
    )
    assert locked["path"] == "stop_first"
    assert locked["gross_pnl"] == -1.5
    assert locked["execution_verdict"] == "faithful"


def test_score_uses_comparable_trials_per_currency() -> None:
    aud = {
        "status": "closed",
        "entry_captured_at_open": True,
        "execution_verdict": "faithful",
        "counts_for_method": True,
        "strategy_id": "scalp:chase@book-v1",
        "symbol": "CBA",
        "currency": "AUD",
        "gross_pnl": -1.0,
        "path": "stop_first",
        "holding_minutes": 3.2,
        "fees_known": False,
        "cause": "faithful_close",
    }
    rows = [dict(aud) for _ in range(30)]
    rows.append(
        {
            **aud,
            "symbol": "SPY",
            "currency": "USD",
            "gross_pnl": 5.0,
            "path": "target_first",
            "holding_minutes": 20,
        }
    )
    rows.append(
        {
            **aud,
            "symbol": "BHP",
            "entry_captured_at_open": False,
            "counts_for_method": False,
            "gross_pnl": -100,
        }
    )
    rows.append(
        {
            **aud,
            "execution_verdict": "broken",
            "counts_for_method": False,
            "gross_pnl": -50,
            "cause": "protective_stop_sent_as_limit",
            "path": "stop_only",
        }
    )
    report = trial_feedback(rows)
    assert report["comparable_count"] == 31
    assert report["excluded"]["entry_not_captured_at_open"] == 1
    assert report["excluded"]["execution_broken"] == 1
    assert report["auto_applied"] is False
    by_ccy = {row["currency"]: row for row in report["by_strategy"]}
    assert by_ccy["AUD"]["gross_pnl"] == -30
    assert by_ccy["AUD"]["path_counts"]["stop_first"] == 30
    assert by_ccy["USD"]["gross_pnl"] == 5
    drafts = [row for row in report["proposals"] if row["action"] == "draft_next_strategy"]
    assert len(drafts) == 1
    assert drafts[0]["currency"] == "AUD"
    assert drafts[0]["applied"] is False
    assert "stop_first 30" in drafts[0]["reason"]
    assert "mean hold 3.2 min" in drafts[0]["reason"]


@pytest.mark.asyncio
async def test_open_trial_records_the_exit_path(session: AsyncSession) -> None:
    clear_settings_cache()
    settings = Settings(universe_mode="dynamic", trade_allowlist=["SPY"])
    session.add(
        WatchlistSymbol(symbol="SPY", horizon="scalp", status="active", priority=80, thesis="t")
    )
    await session.flush()
    mon = PositionMonitor(session, settings=settings)
    opened = await mon.ensure_lifecycle_from_broker(
        symbol="SPY", quantity=1, avg_entry=100, currency="USD", venue="US"
    )
    trial = (
        await session.execute(
            select(MethodTrial).where(MethodTrial.position_lifecycle_id == opened.id)
        )
    ).scalar_one()
    assert trial.entry_captured_at_open is True
    assert trial.status == "open"
    assert trial.strategy_id == "scalp:untagged@book-v1"
    assert trial.stop_price is not None and trial.stop_price < 100
    assert trial.target_price is not None and trial.target_price > 100
    assert trial.intended_hold_minutes == 240

    listed = (
        await session.execute(select(WatchlistSymbol).where(WatchlistSymbol.symbol == "SPY"))
    ).scalar_one()
    listed.horizon = "short"
    await session.flush()
    await mon.ensure_lifecycle_from_broker(
        symbol="SPY", quantity=1, avg_entry=100, currency="USD", venue="US"
    )
    await session.refresh(trial)
    assert trial.strategy_id == "scalp:untagged@book-v1"

    start = opened.opened_at or datetime.now(UTC)
    session.add(
        PositionSnapshotRecord(
            position_lifecycle_id=opened.id,
            symbol="SPY",
            quantity=1,
            market_value=101,
            current_price=101,
            as_of=start + timedelta(minutes=1),
        )
    )
    buy_id = uuid4()
    sell_id = uuid4()
    session.add_all(
        [
            Order(
                id=buy_id,
                symbol="SPY",
                side="buy",
                qty=1,
                order_type="limit",
                status="FILLED",
                idempotency_key=f"buy-{buy_id}",
                raw_payload={"venue": "US", "currency": "USD"},
            ),
            Order(
                id=sell_id,
                symbol="SPY",
                side="sell",
                qty=1,
                order_type="stop",
                status="FILLED",
                idempotency_key=f"protect-stop:{opened.id}",
                raw_payload={"venue": "US", "currency": "USD"},
            ),
            Execution(
                order_id=buy_id,
                symbol="SPY",
                qty=1,
                price=100,
                executed_at=start,
                raw_payload={"venue": "US", "currency": "USD"},
            ),
            Execution(
                order_id=sell_id,
                symbol="SPY",
                qty=1,
                price=100.5,
                executed_at=start + timedelta(minutes=4),
                raw_payload={"venue": "US", "currency": "USD"},
            ),
        ]
    )
    await session.flush()
    done = await complete_method_trial(session, opened)
    assert done is not None
    assert done.counts_for_method is True
    assert done.gross_pnl == 0.5
    assert done.fees_known is False
    assert done.path == "target_only"
    assert done.execution_verdict == "faithful"
    assert done.holding_minutes == 4
    assert done.strategy_verdict == "win"
    again = await complete_method_trial(session, opened)
    assert again is not None
    assert again.gross_pnl == 0.5
    assert again.path == "target_only"


@pytest.mark.asyncio
async def test_position_already_open_is_stored_and_not_scored(session: AsyncSession) -> None:
    clear_settings_cache()
    settings = Settings(universe_mode="dynamic", trade_allowlist=["SPY"])
    session.add(
        WatchlistSymbol(symbol="SPY", horizon="scalp", status="active", priority=80, thesis="t")
    )
    started = datetime(2026, 9, 1, tzinfo=UTC)
    lifecycle = PositionLifecycle(
        id=uuid4(),
        symbol="SPY",
        status="OPEN",
        quantity=1,
        average_entry_price=100,
        venue="US",
        currency="USD",
        opened_at=started,
    )
    session.add(lifecycle)
    await session.flush()
    mon = PositionMonitor(session, settings=settings)
    await mon.ensure_lifecycle_from_broker(
        symbol="SPY", quantity=1, avg_entry=100, currency="USD", venue="US"
    )
    trial = (
        await session.execute(
            select(MethodTrial).where(MethodTrial.position_lifecycle_id == lifecycle.id)
        )
    ).scalar_one()
    assert trial.entry_captured_at_open is False
    buy_id = uuid4()
    sell_id = uuid4()
    session.add_all(
        [
            Order(
                id=buy_id,
                symbol="SPY",
                side="buy",
                qty=1,
                order_type="limit",
                status="FILLED",
                idempotency_key=f"buy-{buy_id}",
                raw_payload={"venue": "US", "currency": "USD"},
            ),
            Order(
                id=sell_id,
                symbol="SPY",
                side="sell",
                qty=1,
                order_type="stop",
                status="FILLED",
                idempotency_key=f"protect-stop:{lifecycle.id}",
                raw_payload={"venue": "US", "currency": "USD"},
            ),
            Execution(
                order_id=buy_id,
                symbol="SPY",
                qty=1,
                price=100,
                executed_at=started,
                raw_payload={"venue": "US", "currency": "USD"},
            ),
            Execution(
                order_id=sell_id,
                symbol="SPY",
                qty=1,
                price=99,
                executed_at=started + timedelta(minutes=10),
                raw_payload={"venue": "US", "currency": "USD"},
            ),
        ]
    )
    await session.flush()
    done = await complete_method_trial(session, lifecycle)
    assert done is not None
    assert done.status == "closed"
    assert done.gross_pnl == -1
    assert done.counts_for_method is False
