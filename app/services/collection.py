"""Orchestrate premarket-style data collection, normalize, and persist."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.earnings import get_earnings_provider
from app.collectors.macro_data import get_macro_data_provider
from app.collectors.market_data import get_market_data_provider
from app.collectors.news import get_news_provider
from app.collectors.sec_filings import get_sec_filings_provider
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.normalize import (
    NormalizedMacroSnapshot,
    NormalizedMarketSnapshot,
    NormalizedNews,
    aggregate_data_quality,
    normalize_macro,
    normalize_market_quote,
    normalize_news_item,
)
from app.services.universe import EligibilityResult, evaluate_symbol_eligibility
from app.storage.repositories import (
    MacroSnapshotRepository,
    MarketSnapshotRepository,
    NewsRepository,
    SystemEventRepository,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class CollectionBundle:
    workflow_id: UUID
    collected_at: datetime
    news: list[NormalizedNews] = field(default_factory=list)
    markets: list[NormalizedMarketSnapshot] = field(default_factory=list)
    macro: NormalizedMacroSnapshot | None = None
    earnings: list[dict[str, Any]] = field(default_factory=list)
    filings: list[dict[str, Any]] = field(default_factory=list)
    eligibility: list[EligibilityResult] = field(default_factory=list)
    aggregate_quality: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def fail_closed(self) -> bool:
        """True when data is too weak to take new risk."""
        if self.errors:
            return True
        if self.macro is None:
            return True
        if not self.markets:
            return True
        if self.aggregate_quality < 0.6:
            return True
        return False


class DataCollectionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        persist: bool = True,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.persist = persist
        self.news_repo = NewsRepository(session)
        self.market_repo = MarketSnapshotRepository(session)
        self.macro_repo = MacroSnapshotRepository(session)
        self.events = SystemEventRepository(session)

    async def collect_premarket(
        self,
        *,
        symbols: list[str] | None = None,
        workflow_id: UUID | None = None,
        horizon_by_symbol: dict[str, str] | None = None,
    ) -> CollectionBundle:
        wf = workflow_id or uuid4()
        now = datetime.now(UTC)
        universe = symbols or list(self.settings.trade_allowlist)
        horizons = {k.upper(): v for k, v in (horizon_by_symbol or {}).items()}
        bundle = CollectionBundle(workflow_id=wf, collected_at=now)

        try:
            raw_news = await get_news_provider().fetch_news(symbols=universe, limit=100)
            seen: set[str] = set()
            for raw in raw_news:
                norm = normalize_news_item(raw, collected_at=now, now=now, seen_hashes=seen)
                bundle.news.append(norm)
                if self.persist and not norm.is_duplicate:
                    await self.news_repo.upsert_normalized(norm)
        except Exception as exc:  # noqa: BLE001 — record and fail closed
            msg = f"news_collection_failed: {exc}"
            bundle.errors.append(msg)
            logger.exception("news_collection_failed", workflow_id=str(wf))
            if self.persist:
                await self.events.record(
                    level="error",
                    event_type="collector_news_error",
                    message=msg,
                    workflow_id=wf,
                )

        try:
            raw_quotes = await get_market_data_provider().fetch_quotes(universe)
            for raw in raw_quotes:
                norm = normalize_market_quote(raw, now=now)
                bundle.markets.append(norm)
                if self.persist:
                    await self.market_repo.add(norm)
                bundle.eligibility.append(
                    evaluate_symbol_eligibility(
                        norm,
                        settings=self.settings,
                        horizon=horizons.get(norm.symbol.upper()),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            msg = f"market_collection_failed: {exc}"
            bundle.errors.append(msg)
            logger.exception("market_collection_failed", workflow_id=str(wf))
            if self.persist:
                await self.events.record(
                    level="error",
                    event_type="collector_market_error",
                    message=msg,
                    workflow_id=wf,
                )

        try:
            raw_macro = await get_macro_data_provider().fetch_macro()
            bundle.macro = normalize_macro(raw_macro, now=now)
            if self.persist and bundle.macro is not None:
                await self.macro_repo.add(bundle.macro)
        except Exception as exc:  # noqa: BLE001
            msg = f"macro_collection_failed: {exc}"
            bundle.errors.append(msg)
            logger.exception("macro_collection_failed", workflow_id=str(wf))
            if self.persist:
                await self.events.record(
                    level="error",
                    event_type="collector_macro_error",
                    message=msg,
                    workflow_id=wf,
                )

        try:
            earnings = await get_earnings_provider().fetch_earnings(universe)
            bundle.earnings = [
                {
                    "symbol": e.symbol,
                    "report_date": e.report_date.isoformat(),
                    "period": e.period,
                    "eps_actual": e.eps_actual,
                    "eps_estimate": e.eps_estimate,
                    "provider": e.provider,
                }
                for e in earnings
            ]
            filings = await get_sec_filings_provider().fetch_filings(universe)
            bundle.filings = [
                {
                    "symbol": f.symbol,
                    "filed_at": f.filed_at.isoformat(),
                    "form_type": f.form_type,
                    "title": f.title,
                    "provider": f.provider,
                }
                for f in filings
            ]
        except Exception as exc:  # noqa: BLE001
            msg = f"fundamentals_collection_failed: {exc}"
            bundle.errors.append(msg)
            logger.exception("fundamentals_collection_failed", workflow_id=str(wf))

        news_scores = [n.quality_score for n in bundle.news if not n.is_duplicate]
        market_scores = [m.quality_score for m in bundle.markets]
        macro_score = bundle.macro.quality_score if bundle.macro else None
        bundle.aggregate_quality = aggregate_data_quality(news_scores, market_scores, macro_score)

        # Never collect stub/fixture prints when the live/order path is on.
        from app.market.live_prices import is_simulation_price_provider, requires_live_market_prices

        if requires_live_market_prices(self.settings):
            if any(is_simulation_price_provider(m.provider) for m in bundle.markets):
                msg = "stub_quotes_blocked_while_live_prices_required"
                bundle.errors.append(msg)
                logger.error(msg, workflow_id=str(wf), markets=len(bundle.markets))
            if not bundle.markets:
                msg = "live_market_quotes_missing"
                bundle.errors.append(msg)
                logger.error(msg, workflow_id=str(wf))

        if bundle.fail_closed and self.persist:
            await self.events.record(
                level="warning",
                event_type="collection_fail_closed",
                message="Premarket collection quality insufficient for new risk",
                context={
                    "aggregate_quality": bundle.aggregate_quality,
                    "errors": bundle.errors,
                    "markets": len(bundle.markets),
                    "news": len(bundle.news),
                },
                workflow_id=wf,
            )

        logger.info(
            "premarket_collection_complete",
            workflow_id=str(wf),
            news=len(bundle.news),
            markets=len(bundle.markets),
            quality=bundle.aggregate_quality,
            fail_closed=bundle.fail_closed,
        )
        return bundle
