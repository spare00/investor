"""Phase 4 data collection pipeline (providers → canonical → quality → events)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.canonical.models import (
    CanonicalEconomicEvent,
    CanonicalMarketSnapshot,
    CanonicalNewsItem,
    CanonicalPremarketSnapshot,
    CanonicalQuote,
    CanonicalSecFiling,
    ConflictState,
    FreshnessState,
    PremarketAvailability,
)
from app.collectors.base import RawMacroSnapshot, RawMarketQuote, RawNewsItem
from app.context_builders.builders import (
    IntradayContextBuilder,
    MacroContextBuilder,
    MarketIntelligenceContextBuilder,
    QuantContextBuilder,
    RevalidationContextBuilder,
)
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.data_quality.news_dedup import cluster_news
from app.data_quality.service import (
    compare_quotes,
    freshness_state_for_quote,
    score_quality,
    session_phase_now,
    surprise,
    validate_bar,
    validate_quote,
)
from app.events.market_events import build_market_events
from app.providers.registry import (
    resolve_macro_provider,
    resolve_market_provider,
    resolve_news_provider,
    resolve_sec_provider,
)
from app.services.collection import CollectionBundle, DataCollectionService
from app.services.normalize import (
    normalize_macro,
    normalize_market_quote,
    normalize_news_item,
    spread_bps,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class DataLayerResult:
    collection_run_id: UUID
    collection_type: str
    status: str
    started_at: datetime
    completed_at: datetime
    quotes: list[CanonicalQuote] = field(default_factory=list)
    bars: list[Any] = field(default_factory=list)
    premarket: list[CanonicalPremarketSnapshot] = field(default_factory=list)
    news: list[CanonicalNewsItem] = field(default_factory=list)
    news_clusters: list[Any] = field(default_factory=list)
    filings: list[CanonicalSecFiling] = field(default_factory=list)
    economic_events: list[CanonicalEconomicEvent] = field(default_factory=list)
    macro: dict[str, Any] = field(default_factory=dict)
    conflicts: list[Any] = field(default_factory=list)
    market_events: list[dict[str, Any]] = field(default_factory=list)
    quality_summary: dict[str, Any] = field(default_factory=dict)
    provider_metas: list[dict[str, Any]] = field(default_factory=list)
    contexts: dict[str, Any] = field(default_factory=dict)
    legacy_bundle: CollectionBundle | None = None
    fail_closed: bool = False
    fail_closed_reasons: list[str] = field(default_factory=list)
    brokers_orders: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_run_id": str(self.collection_run_id),
            "collection_type": self.collection_type,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "counts": {
                "quotes": len(self.quotes),
                "bars": len(self.bars),
                "premarket": len(self.premarket),
                "news": len(self.news),
                "filings": len(self.filings),
                "economic_events": len(self.economic_events),
                "conflicts": len(self.conflicts),
                "market_events": len(self.market_events),
            },
            "quality_summary": self.quality_summary,
            "fail_closed": self.fail_closed,
            "fail_closed_reasons": self.fail_closed_reasons,
            "broker_orders": False,
            "provider_metas": self.provider_metas,
        }


class DataCollectionPipeline:
    """Orchestrates adapters; never calls broker or LLM."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        fixture_mode: bool = False,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.fixture_mode = fixture_mode or not self.settings.enable_external_data

    async def collect(
        self,
        collection_type: str,
        *,
        symbols: list[str] | None = None,
        workflow_id: UUID | None = None,
        cutoff: datetime | None = None,
        venue: str | None = None,
    ) -> DataLayerResult:
        from app.market.venues import Venue, parse_venue

        started = datetime.now(UTC)
        run_id = uuid4()
        wf = workflow_id or run_id
        book = parse_venue(venue)
        if symbols is not None:
            universe = list(symbols)
        elif book == Venue.AU:
            universe = list(self.settings.trade_allowlist_au)
        else:
            universe = list(self.settings.trade_allowlist)
        from app.market.book_context import index_symbols_for_venue

        indexes = list(index_symbols_for_venue(book or Venue.US, self.settings))
        for s in indexes:
            if s not in universe:
                universe = [s, *universe]

        result = DataLayerResult(
            collection_run_id=run_id,
            collection_type=collection_type,
            status="running",
            started_at=started,
            completed_at=started,
        )
        phase = session_phase_now(self.settings, started)
        metas: list[dict[str, Any]] = []

        # Market
        market = resolve_market_provider(self.settings) if not self.fixture_mode else resolve_market_provider(
            # force fixture by temporarily treating as disabled path
            self.settings
        )
        if self.fixture_mode:
            from app.providers.registry import FixtureMarketDataProvider

            market = FixtureMarketDataProvider(allow_offline=True)
        quotes, meta_q = await market.fetch_quotes(universe, settings=self.settings)
        metas.append(meta_q.to_dict())
        bars, meta_b = await market.fetch_daily_bars(universe[:8], settings=self.settings)
        metas.append(meta_b.to_dict())
        premarket, meta_p = await market.fetch_premarket(universe[:8], settings=self.settings)
        metas.append(meta_p.to_dict())

        # News
        news_p = resolve_news_provider(self.settings)
        if self.fixture_mode:
            from app.providers.registry import FixtureNewsProvider

            news_p = FixtureNewsProvider()
        news_raw, meta_n = await news_p.fetch_news(symbols=universe, limit=50, settings=self.settings)
        metas.append(meta_n.to_dict())
        news, clusters = cluster_news(news_raw)

        # SEC EDGAR is US-only. Fake fixture 8-Ks on ASX names halt the AU book.
        from app.providers.base import ProviderRequestMeta, ProviderStatus

        skip_sec = book == Venue.AU or (
            not self.fixture_mode and not self.settings.enable_sec_collection
        )
        if skip_sec:
            now_sec = datetime.now(UTC)
            filings = []
            meta_s = ProviderRequestMeta(
                provider_name="sec_skipped",
                provider_version="1.0.0",
                request_id=str(uuid4()),
                request_started_at=now_sec,
                request_completed_at=now_sec,
                status=ProviderStatus.DISABLED,
            )
        else:
            sec_p = resolve_sec_provider(self.settings)
            if self.fixture_mode:
                from app.providers.registry import FixtureSecProvider

                sec_p = FixtureSecProvider()
            filings, meta_s = await sec_p.fetch_filings(symbols=universe[:5], settings=self.settings)
        metas.append(meta_s.to_dict())

        # Macro
        macro_p = resolve_macro_provider(self.settings)
        macro_dict, meta_m = await macro_p.fetch_macro(settings=self.settings)
        metas.append(meta_m.to_dict())
        economic, meta_e = await macro_p.fetch_economic_calendar(settings=self.settings)
        metas.append(meta_e.to_dict())
        for ev in economic:
            if ev.actual is not None:
                val, direction = surprise(ev.actual, ev.consensus)
                ev.surprise_value = val
                ev.surprise_direction = direction

        # Validate + freshness
        stale_symbols: list[str] = []
        for q in quotes:
            ok, issues = validate_quote(q)
            fres = freshness_state_for_quote(q.as_of, now=started, settings=self.settings, session_phase=phase)
            q.freshness = fres
            q.quality = score_quality(
                freshness=fres,
                completeness=0.9 if q.bid and q.ask else 0.7,
                source_reliability=0.7 if self.fixture_mode else 0.9,
                validation_ok=ok,
                issues=issues,
            )
            if fres in {FreshnessState.EXPIRED, FreshnessState.STALE} and phase == "REGULAR":
                stale_symbols.append(q.symbol)

        for b in bars:
            ok, issues = validate_bar(b.open, b.high, b.low, b.close, b.volume)
            if not ok and b.quality:
                b.quality.issues.extend(issues)
                b.quality.validation = 0.0

        # Single-source conflicts placeholder (fixture only one provider)
        conflicts = []
        if len(quotes) >= 1:
            # Demonstrate SINGLE_SOURCE_ONLY for indexes when no secondary
            from app.canonical.models import CanonicalDataConflict

            for q in quotes:
                if q.symbol in indexes:
                    conflicts.append(
                        CanonicalDataConflict(
                            data_type="quote",
                            symbol_or_key=q.symbol,
                            state=ConflictState.SINGLE_SOURCE_ONLY,
                            primary_value=q.last,
                            provider_names=[q.provenance.provider_name if q.provenance else "unknown"],
                        )
                    )

        events = build_market_events(
            news=news,
            filings=filings,
            economic=economic,
            premarket=premarket,
            conflicts=conflicts,
            stale_symbols=stale_symbols,
            now=started,
        )

        # Fail closed checks
        reasons: list[str] = []
        index_quotes = {q.symbol: q for q in quotes if q.symbol in indexes}
        need = 1 if book == Venue.AU else len(indexes)
        if len(index_quotes) < need:
            reasons.append("missing_core_index_data")
        hard = self.settings.data_quality_hard_fail_threshold
        for sym in indexes:
            q = index_quotes.get(sym)
            if q and q.quality and q.quality.overall < hard:
                reasons.append(f"quality_hard_fail:{sym}")
            if q and q.freshness == FreshnessState.EXPIRED and phase == "REGULAR":
                reasons.append(f"quote_expired:{sym}")
        for c in conflicts:
            if c.state == ConflictState.MATERIAL_CONFLICT:
                reasons.append(f"material_conflict:{c.symbol_or_key}")
        if any(m.get("status") == "error" for m in metas) and not quotes:
            reasons.append("providers_failed_minimum_unmet")

        from app.market.paper_gates import paper_relaxed_data_gates, relax_fail_closed_reasons

        reasons, paper_warnings = relax_fail_closed_reasons(
            reasons, quote_count=len(quotes), settings=self.settings
        )

        # Build legacy CollectionBundle for AgentPipeline compatibility
        legacy = await self._to_legacy_bundle(
            wf, started, quotes, bars, news, filings, macro_dict, economic, reasons
        )
        await self._persist_market_prints(legacy.markets if legacy else [])

        # Contexts
        mi = MarketIntelligenceContextBuilder(self.settings).build(
            news=news, filings=filings, conflicts=conflicts, cutoff=cutoff
        )
        macro_ctx = MacroContextBuilder().build(macro=macro_dict, economic_events=economic, cutoff=cutoff)
        quant_ctx = QuantContextBuilder().build(
            snapshots=[
                CanonicalMarketSnapshot(
                    as_of=q.as_of,
                    collected_at=q.collected_at,
                    symbol=q.symbol,
                    quote=q,
                    indicators={
                        "spread_bps": q.spread_bps,
                        "gap_pct": next(
                            (p.gap_from_previous_close_pct for p in premarket if p.symbol == q.symbol),
                            None,
                        ),
                    },
                    calculation_version=self.settings.calculation_version,
                    input_snapshot_id=str(q.id),
                )
                for q in quotes
            ],
            indicators={"calculation_version": self.settings.calculation_version},
        )
        freshness_map = {q.symbol: q.freshness.value for q in quotes}
        reval_ctx = RevalidationContextBuilder().build(
            quotes=quotes,
            premarket=premarket,
            events=events,
            freshness=freshness_map,
            conflicts=conflicts,
        )
        intra_ctx = IntradayContextBuilder().build(events=events, quotes=quotes)

        qualities = [q.quality.overall for q in quotes if q.quality]
        result.quotes = quotes
        result.bars = bars
        result.premarket = premarket
        result.news = news
        result.news_clusters = clusters
        result.filings = filings
        result.economic_events = economic
        result.macro = macro_dict
        result.conflicts = conflicts
        result.market_events = events
        result.provider_metas = metas
        result.contexts = {
            "market_intelligence": mi,
            "macro": macro_ctx,
            "quant": quant_ctx,
            "revalidation": reval_ctx,
            "intraday": intra_ctx,
        }
        result.quality_summary = {
            "overall": sum(qualities) / len(qualities) if qualities else 0.0,
            "warning_threshold": self.settings.data_quality_warning_threshold,
            "hard_fail_threshold": hard,
            "components_sample": quotes[0].quality.to_dict() if quotes and quotes[0].quality else {},
        }
        result.legacy_bundle = legacy
        if paper_relaxed_data_gates(self.settings) and quotes:
            result.fail_closed = bool(reasons)
            result.fail_closed_reasons = list(reasons)
        else:
            result.fail_closed = bool(reasons) or (legacy.fail_closed if legacy else True)
            result.fail_closed_reasons = reasons or (
                list(legacy.errors) if legacy and legacy.fail_closed else []
            )
        result.status = "failed" if result.fail_closed and "providers_failed" in ",".join(reasons) else (
            "completed_with_warnings" if result.fail_closed else "completed"
        )
        if result.fail_closed and not reasons and legacy:
            result.status = "completed_fail_closed"
        result.completed_at = datetime.now(UTC)
        logger.info(
            "data_collection_done",
            run_id=str(run_id),
            collection_type=collection_type,
            fail_closed=result.fail_closed,
            reasons=result.fail_closed_reasons,
            paper_warnings=paper_warnings,
        )
        return result

    async def _persist_market_prints(self, markets: list[Any]) -> None:
        """Write normalized quotes to market_snapshots for decision-eval density."""
        if not markets:
            return
        from app.storage.repositories import MarketSnapshotRepository

        repo = MarketSnapshotRepository(self.session)
        for item in markets:
            try:
                await repo.add(item)
            except Exception:  # noqa: BLE001
                logger.exception("market_snapshot_persist_failed", symbol=getattr(item, "symbol", None))

    async def _to_legacy_bundle(
        self,
        workflow_id: UUID,
        now: datetime,
        quotes: list[CanonicalQuote],
        bars: list[Any],
        news: list[CanonicalNewsItem],
        filings: list[CanonicalSecFiling],
        macro_dict: dict[str, Any],
        economic: list[CanonicalEconomicEvent],
        reasons: list[str],
    ) -> CollectionBundle:
        """Adapt canonical → existing CollectionBundle for AgentPipeline."""
        # Prefer existing DataCollectionService path for eligibility + persist when fixture
        # Build Raw* and normalize to keep agent schemas happy
        seen: set[str] = set()
        norm_news = []
        for n in news:
            raw = RawNewsItem(
                headline=n.headline,
                source=n.source_name,
                published_at=n.published_at,
                provider=n.provenance.provider_name if n.provenance else "fixture",
                external_id=n.provider_article_id,
                url=n.source_url_reference,
                symbols=n.symbols,
                category=n.categories[0] if n.categories else None,
                raw_payload={"canonical_id": str(n.id), "provenance": n.provenance.model_dump(mode="json") if n.provenance else {}},
            )
            norm_news.append(normalize_news_item(raw, collected_at=now, now=now, seen_hashes=seen))

        markets = []
        bar_by_sym = {b.symbol: b for b in bars}
        for q in quotes:
            b = bar_by_sym.get(q.symbol)
            raw = RawMarketQuote(
                symbol=q.symbol,
                as_of=q.as_of,
                provider=q.provenance.provider_name if q.provenance else "fixture",
                last=q.last,
                open=b.open if b else q.last,
                high=b.high if b else q.last,
                low=b.low if b else q.last,
                volume=b.volume if b else None,
                bid=q.bid,
                ask=q.ask,
                gap_pct=None,
                raw_payload={"canonical_id": str(q.id)},
            )
            markets.append(normalize_market_quote(raw, now=now))

        raw_macro = RawMacroSnapshot(
            as_of=now,
            provider="fixture",
            fed_funds_rate=macro_dict.get("fed_funds_rate"),
            cpi_yoy=macro_dict.get("cpi_yoy"),
            pce_yoy=macro_dict.get("pce_yoy"),
            unemployment_rate=macro_dict.get("unemployment_rate"),
            gdp_growth_q_o_q=macro_dict.get("gdp_growth_q_o_q"),
            us_10y_yield=macro_dict.get("us_10y_yield"),
            us_2y_yield=macro_dict.get("us_2y_yield"),
            dxy=macro_dict.get("dxy"),
            wti_oil=macro_dict.get("wti_oil"),
            gold=macro_dict.get("gold"),
            hy_credit_spread_bps=macro_dict.get("hy_credit_spread_bps"),
            notes=["phase4_pipeline"],
            raw_payload={"economic_events": [e.event_id for e in economic]},
        )
        norm_macro = normalize_macro(raw_macro, now=now)

        # Use DataCollectionService aggregate + eligibility via a light collect
        # Build bundle manually
        from app.services.collection import CollectionBundle
        from app.services.normalize import aggregate_data_quality
        from app.services.universe import evaluate_symbol_eligibility

        eligibility = [
            evaluate_symbol_eligibility(m, settings=self.settings) for m in markets
        ]
        bundle = CollectionBundle(
            workflow_id=workflow_id,
            collected_at=now,
            news=norm_news,
            markets=markets,
            macro=norm_macro,
            earnings=[],
            filings=[
                {
                    "symbol": (f.symbols[0] if f.symbols else ""),
                    "form_type": f.form_type,
                    "filed_at": f.filed_at.isoformat(),
                    "accession": f.accession_number,
                    "title": f.company_name,
                    "url": f.document_url_reference,
                    "summary": ",".join(f.importance_hints),
                    "provenance": f.provenance.model_dump(mode="json") if f.provenance else {},
                }
                for f in filings
            ],
            eligibility=eligibility,
            aggregate_quality=aggregate_data_quality(
                [n.quality_score for n in norm_news],
                [m.quality_score for m in markets],
                norm_macro.quality_score if norm_macro else None,
            ),
            errors=list(reasons),
        )
        return bundle
