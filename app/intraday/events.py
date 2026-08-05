"""Intraday event bus with deduplication and rate control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import IntradayEvent


PRIORITY: dict[str, int] = {
    "EMERGENCY_STOP_TRIGGERED": 100,
    "RISK_LIMIT_BREACH": 95,
    "STOP_TRIGGERED": 90,
    "INVALIDATION_TRIGGERED": 88,
    "TRADING_HALT": 85,
    "DATA_STALE": 80,
    "ORDER_REJECTED": 75,
    "TAKE_PROFIT_TRIGGERED": 70,
    "HIGH_IMPORTANCE_NEWS": 65,
    "CLOSING_WINDOW_ENTERED": 60,
    "RISK_LIMIT_WARNING": 55,
    "POSITION_CHANGED": 50,
    "BROKER_ORDER_UPDATE": 45,
    "MARKET_DATA_UPDATE": 20,
}


@dataclass(slots=True)
class EventPublishResult:
    event_id: str
    status: str  # NEW | DEDUPLICATED
    priority: int
    requires_analysis: bool


class IntradayEventBus:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._reanalysis_times: list[datetime] = []
        self._symbol_reanalysis: dict[str, list[datetime]] = {}

    async def publish(
        self,
        *,
        event_type: str,
        source: str,
        symbols: list[str] | None = None,
        importance: str = "medium",
        deduplication_key: str,
        requires_analysis: bool = False,
        requires_risk_review: bool = False,
        requires_execution_review: bool = False,
        payload: dict[str, Any] | None = None,
        workflow_run_id: UUID | None = None,
        decision_id: UUID | None = None,
        intent_id: UUID | None = None,
        order_id: UUID | None = None,
        position_id: UUID | None = None,
        source_event_id: str | None = None,
        bypass_cooldown: bool = False,
    ) -> EventPublishResult:
        now = datetime.now(UTC)
        window = timedelta(seconds=self.settings.event_deduplication_window_seconds)
        existing = (
            await self.session.execute(
                select(IntradayEvent)
                .where(IntradayEvent.deduplication_key == deduplication_key)
                .where(IntradayEvent.detected_at >= now - window)
                .order_by(IntradayEvent.detected_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = "DEDUPLICATED"
            existing.revision = int(existing.revision or 1) + 1
            await self.session.flush()
            return EventPublishResult(
                event_id=str(existing.id),
                status="DEDUPLICATED",
                priority=PRIORITY.get(event_type, 10),
                requires_analysis=False,
            )

        ev = IntradayEvent(
            id=uuid4(),
            event_type=event_type,
            source=source,
            source_event_id=source_event_id,
            workflow_run_id=workflow_run_id,
            decision_id=decision_id,
            intent_id=intent_id,
            order_id=order_id,
            position_id=position_id,
            symbols=symbols or [],
            importance=importance,
            detected_at=now,
            effective_at=now,
            expires_at=now + timedelta(hours=6),
            deduplication_key=deduplication_key,
            requires_analysis=requires_analysis,
            requires_risk_review=requires_risk_review,
            requires_execution_review=requires_execution_review,
            payload=payload or {},
            status="NEW",
            revision=1,
            priority=PRIORITY.get(event_type, 10),
            bypass_cooldown=bypass_cooldown,
        )
        self.session.add(ev)
        await self.session.flush()
        return EventPublishResult(
            event_id=str(ev.id),
            status="NEW",
            priority=ev.priority,
            requires_analysis=requires_analysis,
        )

    def allow_reanalysis(
        self,
        *,
        symbols: list[str],
        bypass: bool = False,
        now: datetime | None = None,
        horizon_by_symbol: dict[str, str] | None = None,
    ) -> tuple[bool, str | None]:
        if bypass:
            return True, None
        from app.universe.reeval import global_reeval_gap_minutes, symbol_reeval_gap_minutes

        now = now or datetime.now(UTC)
        horizons = horizon_by_symbol or {}
        # global max
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_count = sum(1 for t in self._reanalysis_times if t >= day_start)
        if day_count >= self.settings.max_intraday_reanalyses:
            return False, "max_intraday_reanalyses"
        if self._reanalysis_times:
            gap = (now - self._reanalysis_times[-1]).total_seconds() / 60.0
            need = global_reeval_gap_minutes(symbols or ["PORTFOLIO"], horizons, self.settings)
            if gap < need:
                return False, "global_cooldown"
        for sym in symbols:
            times = self._symbol_reanalysis.get(sym.upper(), [])
            day_sym = sum(1 for t in times if t >= day_start)
            if day_sym >= self.settings.max_symbol_reanalyses_per_day:
                return False, f"symbol_max:{sym}"
            need_sym = symbol_reeval_gap_minutes(sym, horizons, self.settings)
            if times and (now - times[-1]).total_seconds() / 60.0 < need_sym:
                return False, f"symbol_cooldown:{sym}"
        return True, None

    def record_reanalysis(self, symbols: list[str], *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._reanalysis_times.append(now)
        for sym in symbols:
            self._symbol_reanalysis.setdefault(sym.upper(), []).append(now)

    async def list_events(self, *, limit: int = 50) -> list[IntradayEvent]:
        return list(
            (
                await self.session.execute(
                    select(IntradayEvent).order_by(IntradayEvent.detected_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def list_pending_actionable(self, *, limit: int = 40) -> list[IntradayEvent]:
        """NEW events that should escalate to risk review / CIO reanalysis."""
        rows = await self.list_events(limit=limit)
        out: list[IntradayEvent] = []
        for ev in rows:
            if ev.status != "NEW":
                continue
            if (
                ev.requires_analysis
                or ev.requires_execution_review
                or ev.requires_risk_review
                or (ev.importance or "").lower() in {"high", "critical"}
            ):
                out.append(ev)
        return out

    async def mark(self, event_id: UUID, status: str) -> IntradayEvent | None:
        row = await self.session.get(IntradayEvent, event_id)
        if row is None:
            return None
        row.status = status
        await self.session.flush()
        return row
