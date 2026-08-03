"""Persistence helpers for collected / normalized data."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    MacroSnapshot,
    MarketSnapshot,
    NewsItem,
    SystemEvent,
)
from app.services.normalize import (
    NormalizedMacroSnapshot,
    NormalizedMarketSnapshot,
    NormalizedNews,
)


class NewsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_normalized(self, item: NormalizedNews) -> NewsItem:
        existing: NewsItem | None = None
        if item.external_id:
            result = await self.session.execute(
                select(NewsItem).where(
                    NewsItem.provider == item.provider,
                    NewsItem.external_id == item.external_id,
                )
            )
            existing = result.scalar_one_or_none()
        if existing is None:
            result = await self.session.execute(
                select(NewsItem).where(NewsItem.headline_hash == item.headline_hash).limit(1)
            )
            existing = result.scalar_one_or_none()

        if existing:
            # Already stored — treat caller item as duplicate / no-op insert.
            return existing

        row = NewsItem(
            provider=item.provider,
            external_id=item.external_id,
            headline=item.headline,
            headline_hash=item.headline_hash,
            source=item.source,
            url=item.url,
            published_at=item.published_at,
            collected_at=item.collected_at,
            symbols=item.symbols,
            category=item.category,
            raw_payload=item.raw_payload,
            freshness_score=item.freshness_score,
            quality_score=item.quality_score,
            is_duplicate=item.is_duplicate,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def recent(self, *, limit: int = 50) -> list[NewsItem]:
        result = await self.session.execute(
            select(NewsItem)
            .where(NewsItem.is_duplicate.is_(False))
            .order_by(NewsItem.published_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


class MarketSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, item: NormalizedMarketSnapshot) -> MarketSnapshot:
        row = MarketSnapshot(
            symbol=item.symbol,
            as_of=item.as_of,
            provider=item.provider,
            last=item.last,
            open=item.open,
            high=item.high,
            low=item.low,
            volume=item.volume,
            avg_volume_20d=item.avg_volume_20d,
            atr_14=item.atr_14,
            rsi_14=item.rsi_14,
            sma_20=item.sma_20,
            sma_50=item.sma_50,
            sma_200=item.sma_200,
            bid=item.bid,
            ask=item.ask,
            spread_bps=item.spread_bps,
            premarket_change_pct=item.premarket_change_pct,
            gap_pct=item.gap_pct,
            vix=item.vix,
            raw_payload=item.raw_payload,
            freshness_score=item.freshness_score,
            quality_score=item.quality_score,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def latest_for_symbols(self, symbols: list[str]) -> list[MarketSnapshot]:
        # Simple approach: fetch recent rows and keep newest per symbol.
        result = await self.session.execute(
            select(MarketSnapshot)
            .where(MarketSnapshot.symbol.in_([s.upper() for s in symbols]))
            .order_by(MarketSnapshot.as_of.desc())
        )
        newest: dict[str, MarketSnapshot] = {}
        for row in result.scalars().all():
            if row.symbol not in newest:
                newest[row.symbol] = row
        return list(newest.values())


class MacroSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, item: NormalizedMacroSnapshot) -> MacroSnapshot:
        row = MacroSnapshot(
            as_of=item.as_of,
            provider=item.provider,
            fed_funds_rate=item.fed_funds_rate,
            cpi_yoy=item.cpi_yoy,
            pce_yoy=item.pce_yoy,
            unemployment_rate=item.unemployment_rate,
            gdp_growth_q_o_q=item.gdp_growth_q_o_q,
            us_10y_yield=item.us_10y_yield,
            us_2y_yield=item.us_2y_yield,
            dxy=item.dxy,
            wti_oil=item.wti_oil,
            gold=item.gold,
            hy_credit_spread_bps=item.hy_credit_spread_bps,
            notes=item.notes,
            raw_payload=item.raw_payload,
            freshness_score=item.freshness_score,
            quality_score=item.quality_score,
        )
        self.session.add(row)
        await self.session.flush()
        return row


class SystemEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        context: dict[str, Any] | None = None,
        workflow_id: UUID | None = None,
    ) -> SystemEvent:
        row = SystemEvent(
            level=level,
            event_type=event_type,
            message=message,
            context=context or {},
            workflow_id=workflow_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row
