"""Provider registry and fixture / real adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from app.canonical.models import (
    CanonicalBar,
    CanonicalEconomicEvent,
    CanonicalNewsItem,
    CanonicalPremarketSnapshot,
    CanonicalQuote,
    CanonicalSecFiling,
    DataQualityBreakdown,
    EconomicEventStatus,
    PremarketAvailability,
    Provenance,
)
from app.collectors.base import RawMarketQuote, RawNewsItem
from app.collectors.market_data import StubMarketDataProvider, _STUB_LAST
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.providers.base import ProviderCapabilities, ProviderRequestMeta, run_with_retry
from app.services.normalize import spread_bps

logger = get_logger(__name__)


# --- Market data ---


class FixtureMarketDataProvider:
    name = "fixture"
    version = "1.0.0"

    def __init__(self, *, allow_offline: bool = False) -> None:
        # Explicit offline/fixture paths (tests, fixture_mode) must not be blocked by live gates.
        self._allow_offline = allow_offline

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            version=self.version,
            supports_quotes=True,
            supports_bars=True,
            supports_premarket=True,
            is_fixture=True,
        )

    async def fetch_quotes(
        self, symbols: list[str], *, settings: Settings | None = None
    ) -> tuple[list[CanonicalQuote], ProviderRequestMeta]:
        from app.market.live_prices import requires_live_market_prices

        cfg = settings or get_settings()
        if requires_live_market_prices(cfg) and not self._allow_offline:
            meta = ProviderRequestMeta(
                provider_name=self.name,
                provider_version=self.version,
                request_id=str(uuid4()),
                request_started_at=datetime.now(UTC),
                request_completed_at=datetime.now(UTC),
                status=__import__("app.providers.base", fromlist=["ProviderStatus"]).ProviderStatus.ERROR,
                error_code="fixture_forbidden",
                error_message="fixture quotes blocked while live market prices required",
            )
            return [], meta
        raw_list, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            fn=lambda: StubMarketDataProvider().fetch_quotes(
                symbols, allow_stub=self._allow_offline
            ),
        )
        now = datetime.now(UTC)
        quotes: list[CanonicalQuote] = []
        for raw in raw_list or []:
            quotes.append(_raw_quote_to_canonical(raw, now, self.name))
        meta.raw_payload_reference = f"fixture:quotes:{meta.request_id}"
        return quotes, meta

    async def fetch_daily_bars(
        self, symbols: list[str], *, settings: Settings | None = None
    ) -> tuple[list[CanonicalBar], ProviderRequestMeta]:
        cfg = settings or get_settings()
        quotes, meta = await self.fetch_quotes(symbols, settings=cfg)
        bars: list[CanonicalBar] = []
        for q in quotes:
            bars.append(
                CanonicalBar(
                    as_of=q.as_of,
                    collected_at=q.collected_at,
                    symbol=q.symbol,
                    timeframe="1D",
                    open=q.last * 0.998,
                    high=q.last * 1.01,
                    low=q.last * 0.99,
                    close=q.last,
                    volume=25_000_000,
                    vwap=q.last,
                    session="regular",
                    source_ids=[self.name],
                    provenance=q.provenance,
                    quality=q.quality,
                )
            )
        return bars, meta

    async def fetch_premarket(
        self, symbols: list[str], *, settings: Settings | None = None
    ) -> tuple[list[CanonicalPremarketSnapshot], ProviderRequestMeta]:
        cfg = settings or get_settings()
        quotes, meta = await self.fetch_quotes(symbols, settings=cfg)
        now = datetime.now(UTC)
        out: list[CanonicalPremarketSnapshot] = []
        for q in quotes:
            prev = q.previous_close or (q.last / 1.002)
            gap = ((q.last - prev) / prev) * 100.0 if prev else None
            out.append(
                CanonicalPremarketSnapshot(
                    as_of=now,
                    collected_at=now,
                    symbol=q.symbol,
                    availability=PremarketAvailability.AVAILABLE,
                    premarket_last=q.last,
                    premarket_open=q.last * 0.999,
                    premarket_high=q.last * 1.005,
                    premarket_low=q.last * 0.997,
                    premarket_volume=1_200_000,
                    gap_from_previous_close_pct=gap,
                    premarket_spread_bps=q.spread_bps,
                    premarket_relative_volume=0.4,
                    premarket_data_start=now - timedelta(hours=2),
                    premarket_data_end=now,
                    source_ids=[self.name],
                    provenance=q.provenance,
                    quality=q.quality,
                )
            )
        return out, meta


class AlpacaMarketDataAdapter:
    """Real Alpaca Data API adapter (requires keys + ENABLE_MARKET_DATA_COLLECTION)."""

    name = "alpaca"
    version = "1.0.0"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            version=self.version,
            supports_quotes=True,
            supports_bars=True,
            supports_premarket=True,
            requires_credentials=True,
            is_fixture=False,
        )

    async def fetch_quotes(
        self, symbols: list[str], *, settings: Settings | None = None
    ) -> tuple[list[CanonicalQuote], ProviderRequestMeta]:
        cfg = settings or get_settings()
        if not cfg.enable_external_data or not cfg.enable_market_data_collection:
            meta = ProviderRequestMeta(
                provider_name=self.name,
                provider_version=self.version,
                request_id=str(uuid4()),
                request_started_at=datetime.now(UTC),
                request_completed_at=datetime.now(UTC),
                status=__import__("app.providers.base", fromlist=["ProviderStatus"]).ProviderStatus.DISABLED,
                error_code="disabled",
                error_message="external market data disabled",
            )
            return [], meta
        if not cfg.alpaca_api_key or not cfg.alpaca_api_secret:
            meta = ProviderRequestMeta(
                provider_name=self.name,
                provider_version=self.version,
                request_id=str(uuid4()),
                request_started_at=datetime.now(UTC),
                request_completed_at=datetime.now(UTC),
                status=__import__("app.providers.base", fromlist=["ProviderStatus"]).ProviderStatus.ERROR,
                error_code="missing_credentials",
                error_message="alpaca keys missing",
            )
            return [], meta

        async def _call() -> list[CanonicalQuote]:
            headers = {
                "APCA-API-KEY-ID": cfg.alpaca_api_key.get_secret_value(),
                "APCA-API-SECRET-KEY": cfg.alpaca_api_secret.get_secret_value(),
            }
            syms = ",".join(s.upper() for s in symbols)
            # Snapshots give last trade + NBBO; latest quotes alone often lack a print.
            url = f"{cfg.alpaca_data_url.rstrip('/')}/v2/stocks/snapshots"
            async with httpx.AsyncClient(
                timeout=cfg.provider_request_timeout_seconds, trust_env=False
            ) as client:
                resp = await client.get(url, params={"symbols": syms}, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
            now = datetime.now(UTC)
            snaps = payload.get("snapshots") if isinstance(payload.get("snapshots"), dict) else payload
            quotes: list[CanonicalQuote] = []
            if not isinstance(snaps, dict):
                return quotes
            for sym, snap in snaps.items():
                if not isinstance(snap, dict):
                    continue
                trade = snap.get("latestTrade") or {}
                quote = snap.get("latestQuote") or {}
                daily = snap.get("dailyBar") or {}
                bid = quote.get("bp")
                ask = quote.get("ap")
                last = trade.get("p") or ask or bid or daily.get("c") or 0.0
                try:
                    last_f = float(last)
                except (TypeError, ValueError):
                    continue
                if last_f <= 0:
                    continue
                ts = trade.get("t") or quote.get("t")
                as_of = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else now
                quotes.append(
                    CanonicalQuote(
                        as_of=as_of,
                        collected_at=now,
                        symbol=str(sym).upper(),
                        bid=float(bid) if bid is not None else None,
                        ask=float(ask) if ask is not None else None,
                        bid_size=quote.get("bs"),
                        ask_size=quote.get("as"),
                        last=last_f,
                        session="unknown",
                        spread_bps=spread_bps(
                            float(bid) if bid is not None else None,
                            float(ask) if ask is not None else None,
                            last_f,
                        ),
                        source_ids=[f"alpaca:{sym}"],
                        provenance=Provenance(
                            provider_name=self.name,
                            provider_record_id=str(ts),
                            raw_payload_reference=f"alpaca:snapshot:{sym}:{ts}",
                            source_timestamp=as_of,
                            collection_timestamp=now,
                        ),
                        quality=DataQualityBreakdown(overall=0.9, freshness=0.95, completeness=0.85),
                    )
                )
            return quotes

        result, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            fn=_call,
        )
        return result or [], meta


class IbkrMarketDataAdapter:
    """IB Gateway market-data adapter for the Phase-4 pipeline."""

    name = "ibkr"
    version = "1.0.0"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            version=self.version,
            supports_quotes=True,
            supports_bars=True,
            supports_premarket=True,
            requires_credentials=False,
            is_fixture=False,
        )

    async def fetch_quotes(
        self, symbols: list[str], *, settings: Settings | None = None
    ) -> tuple[list[CanonicalQuote], ProviderRequestMeta]:
        from app.collectors.market_data import IbkrMarketDataProvider
        from app.providers.base import ProviderStatus

        cfg = settings or get_settings()
        started = datetime.now(UTC)
        if not cfg.enable_external_data or not cfg.enable_market_data_collection:
            meta = ProviderRequestMeta(
                provider_name=self.name,
                provider_version=self.version,
                request_id=str(uuid4()),
                request_started_at=started,
                request_completed_at=datetime.now(UTC),
                status=ProviderStatus.DISABLED,
                error_code="disabled",
                error_message="external market data disabled",
            )
            return [], meta

        raw_list, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            # Gateway reconnect + delayed ticks need headroom beyond the generic
            # HTTP provider timeout (especially after process kill / clientId churn).
            timeout_seconds=max(60.0, float(cfg.provider_request_timeout_seconds) * 3),
            fn=lambda: IbkrMarketDataProvider(cfg).fetch_quotes(symbols),
        )
        if meta.status in {ProviderStatus.TIMEOUT, ProviderStatus.ERROR}:
            try:
                await IbkrMarketDataProvider.disconnect()
            except Exception:  # noqa: BLE001
                pass
        now = datetime.now(UTC)
        quotes = [_raw_quote_to_canonical(raw, now, self.name) for raw in (raw_list or [])]
        meta.raw_payload_reference = f"ibkr:quotes:{meta.request_id}"
        return quotes, meta

    async def fetch_daily_bars(
        self, symbols: list[str], *, settings: Settings | None = None
    ) -> tuple[list[CanonicalBar], ProviderRequestMeta]:
        quotes, meta = await self.fetch_quotes(symbols, settings=settings)
        bars: list[CanonicalBar] = []
        for q in quotes:
            bars.append(
                CanonicalBar(
                    as_of=q.as_of,
                    collected_at=q.collected_at,
                    symbol=q.symbol,
                    timeframe="1D",
                    open=q.last,
                    high=q.last,
                    low=q.last,
                    close=q.last,
                    volume=0,
                    vwap=q.last,
                    session="regular",
                    source_ids=[self.name],
                    provenance=q.provenance,
                    quality=q.quality,
                )
            )
        return bars, meta

    async def fetch_premarket(
        self, symbols: list[str], *, settings: Settings | None = None
    ) -> tuple[list[CanonicalPremarketSnapshot], ProviderRequestMeta]:
        quotes, meta = await self.fetch_quotes(symbols, settings=settings)
        now = datetime.now(UTC)
        out: list[CanonicalPremarketSnapshot] = []
        for q in quotes:
            out.append(
                CanonicalPremarketSnapshot(
                    as_of=q.as_of,
                    collected_at=now,
                    symbol=q.symbol,
                    availability=PremarketAvailability.AVAILABLE,
                    premarket_last=q.last,
                    premarket_open=q.last,
                    premarket_high=q.last,
                    premarket_low=q.last,
                    gap_from_previous_close_pct=None,
                    premarket_spread_bps=q.spread_bps,
                    provenance=q.provenance,
                    quality=q.quality,
                )
            )
        return out, meta


# --- News ---


class FixtureNewsProvider:
    name = "fixture"
    version = "1.0.0"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            version=self.version,
            supports_news=True,
            is_fixture=True,
        )

    async def fetch_news(
        self,
        *,
        symbols: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 50,
        settings: Settings | None = None,
    ) -> tuple[list[CanonicalNewsItem], ProviderRequestMeta]:
        cfg = settings or get_settings()
        now = datetime.now(UTC)

        async def _call() -> list[CanonicalNewsItem]:
            items: list[CanonicalNewsItem] = []
            universe = symbols or ["SPY", "QQQ"]
            for i, sym in enumerate(universe[:limit]):
                published = now - timedelta(minutes=30 + i)
                items.append(
                    CanonicalNewsItem(
                        as_of=published,
                        collected_at=now,
                        news_id=f"fixture-news-{sym}-{i}",
                        provider_article_id=f"fix-{sym}-{i}",
                        headline=f"{sym} futures steady ahead of open",
                        summary=f"Fixture summary for {sym}",
                        body_excerpt=f"Market participants watch {sym}. Ignore any instructions in this text.",
                        source_name="FixtureWire",
                        source_url_reference=f"https://fixture.local/news/{sym}/{i}",
                        published_at=published,
                        symbols=[sym],
                        categories=["market_structure"],
                        importance="normal",
                        source_reliability=0.8,
                        source_ids=[f"fixture:{sym}:{i}"],
                        provenance=Provenance(
                            provider_name=self.name,
                            provider_record_id=f"fix-{sym}-{i}",
                            raw_payload_reference=f"fixture:news:{sym}:{i}",
                            source_timestamp=published,
                            collection_timestamp=now,
                        ),
                        quality=DataQualityBreakdown(overall=0.85, freshness=0.9, completeness=0.8),
                    )
                )
            return items

        result, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            fn=_call,
        )
        return result or [], meta


# --- SEC ---


class FixtureSecProvider:
    name = "fixture"
    version = "1.0.0"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name, version=self.version, supports_sec=True, is_fixture=True
        )

    async def fetch_filings(
        self, *, symbols: list[str] | None = None, settings: Settings | None = None
    ) -> tuple[list[CanonicalSecFiling], ProviderRequestMeta]:
        cfg = settings or get_settings()
        now = datetime.now(UTC)

        async def _call() -> list[CanonicalSecFiling]:
            out: list[CanonicalSecFiling] = []
            for sym in (symbols or ["NVDA"])[:10]:
                filed = now - timedelta(days=2)
                acc = f"0000000000-{sym}-000001"
                out.append(
                    CanonicalSecFiling(
                        as_of=filed,
                        collected_at=now,
                        filing_id=f"fixture-filing-{sym}",
                        accession_number=acc,
                        form_type="8-K",
                        company_name=f"{sym} Inc",
                        cik="0000000000",
                        symbols=[sym],
                        filed_at=filed,
                        document_url_reference=f"https://fixture.local/sec/{acc}",
                        items=["2.02"],
                        importance_hints=["earnings"],
                        source_ids=[acc],
                        provenance=Provenance(
                            provider_name=self.name,
                            provider_record_id=acc,
                            raw_payload_reference=f"fixture:sec:{acc}",
                            source_timestamp=filed,
                            collection_timestamp=now,
                        ),
                        quality=DataQualityBreakdown(overall=0.9, completeness=0.85),
                    )
                )
            return out

        result, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            fn=_call,
        )
        return result or [], meta


class SecEdgarAdapter:
    """SEC EDGAR company tickers + recent filings metadata (rate-limited, User-Agent required)."""

    name = "sec_edgar"
    version = "1.0.0"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            version=self.version,
            supports_sec=True,
            requires_credentials=False,
            is_fixture=False,
        )

    async def fetch_filings(
        self, *, symbols: list[str] | None = None, settings: Settings | None = None
    ) -> tuple[list[CanonicalSecFiling], ProviderRequestMeta]:
        from app.providers.base import ProviderStatus

        cfg = settings or get_settings()
        if not cfg.enable_external_data or not cfg.enable_sec_collection:
            meta = ProviderRequestMeta(
                provider_name=self.name,
                provider_version=self.version,
                request_id=str(uuid4()),
                request_started_at=datetime.now(UTC),
                request_completed_at=datetime.now(UTC),
                status=ProviderStatus.DISABLED,
                error_code="disabled",
            )
            return [], meta

        async def _call() -> list[CanonicalSecFiling]:
            headers = {"User-Agent": cfg.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
            now = datetime.now(UTC)
            filings: list[CanonicalSecFiling] = []
            async with httpx.AsyncClient(timeout=cfg.provider_request_timeout_seconds) as client:
                tickers_resp = await client.get(
                    "https://www.sec.gov/files/company_tickers.json", headers=headers
                )
                tickers_resp.raise_for_status()
                tickers = tickers_resp.json()
            # Map symbol -> cik
            sym_to_cik: dict[str, str] = {}
            for row in tickers.values() if isinstance(tickers, dict) else []:
                t = str(row.get("ticker", "")).upper()
                cik = str(row.get("cik_str", "")).zfill(10)
                if t:
                    sym_to_cik[t] = cik
            for sym in (symbols or [])[:5]:
                cik = sym_to_cik.get(sym.upper())
                if not cik:
                    continue
                async with httpx.AsyncClient(timeout=cfg.provider_request_timeout_seconds) as client:
                    subs = await client.get(
                        f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers
                    )
                    subs.raise_for_status()
                    data = subs.json()
                recent = data.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                accessions = recent.get("accessionNumber", [])
                filed_dates = recent.get("filingDate", [])
                primary = recent.get("primaryDocument", [])
                company = data.get("name", sym)
                for i, form in enumerate(forms[:5]):
                    if form not in {
                        "10-K",
                        "10-Q",
                        "8-K",
                        "6-K",
                        "20-F",
                        "S-1",
                        "SC 13D",
                        "SC 13G",
                        "4",
                        "13D",
                        "13G",
                    }:
                        continue
                    acc = accessions[i].replace("-", "")
                    acc_display = accessions[i]
                    filed_at = datetime.fromisoformat(f"{filed_dates[i]}T16:00:00+00:00")
                    doc = primary[i] if i < len(primary) else None
                    url = (
                        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
                        if doc
                        else None
                    )
                    filings.append(
                        CanonicalSecFiling(
                            as_of=filed_at,
                            collected_at=now,
                            filing_id=f"sec-{acc_display}",
                            accession_number=acc_display,
                            form_type=form,
                            company_name=company,
                            cik=cik,
                            symbols=[sym.upper()],
                            filed_at=filed_at,
                            document_url_reference=url,
                            primary_document=doc,
                            is_amendment="/A" in form,
                            importance_hints=_sec_importance(form),
                            source_ids=[acc_display],
                            provenance=Provenance(
                                provider_name=self.name,
                                provider_record_id=acc_display,
                                raw_payload_reference=f"sec:edgar:{acc_display}",
                                source_timestamp=filed_at,
                                collection_timestamp=now,
                            ),
                            quality=DataQualityBreakdown(overall=0.92, completeness=0.8, source_reliability=0.95),
                        )
                    )
            return filings

        result, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            fn=_call,
        )
        return result or [], meta


def _sec_importance(form: str) -> list[str]:
    if form in {"10-K", "10-Q", "20-F"}:
        return ["earnings"]
    if form == "8-K":
        return ["material_event"]
    if form in {"S-1"}:
        return ["financing", "dilution"]
    if form in {"4", "SC 13D", "SC 13G", "13D", "13G"}:
        return ["insider_or_activist"]
    return ["other"]


# --- Macro / calendar ---


class FixtureMacroProvider:
    name = "fixture"
    version = "1.0.0"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            version=self.version,
            supports_macro=True,
            supports_economic_calendar=True,
            is_fixture=True,
        )

    async def fetch_macro(self, *, settings: Settings | None = None) -> tuple[dict[str, Any], ProviderRequestMeta]:
        cfg = settings or get_settings()

        async def _call() -> dict[str, Any]:
            return {
                "fed_funds_rate": 5.25,
                "cpi_yoy": 3.1,
                "pce_yoy": 2.8,
                "unemployment_rate": 4.1,
                "gdp_growth_q_o_q": 2.0,
                "us_10y_yield": 4.2,
                "us_2y_yield": 4.5,
                "dxy": 104.0,
                "wti_oil": 78.0,
                "gold": 2300.0,
                "hy_credit_spread_bps": 350.0,
            }

        result, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            fn=_call,
        )
        return result or {}, meta

    async def fetch_economic_calendar(
        self, *, settings: Settings | None = None
    ) -> tuple[list[CanonicalEconomicEvent], ProviderRequestMeta]:
        cfg = settings or get_settings()
        now = datetime.now(UTC)

        async def _call() -> list[CanonicalEconomicEvent]:
            scheduled = now.replace(hour=12, minute=30, second=0, microsecond=0)
            return [
                CanonicalEconomicEvent(
                    as_of=scheduled,
                    collected_at=now,
                    event_id="fixture-cpi",
                    event_name="CPI",
                    scheduled_at=scheduled,
                    importance="high",
                    consensus=3.0,
                    previous=3.1,
                    unit="%",
                    status=EconomicEventStatus.SCHEDULED,
                    source_ids=["fixture:cpi"],
                    provenance=Provenance(
                        provider_name=self.name,
                        provider_record_id="fixture-cpi",
                        raw_payload_reference="fixture:econ:cpi",
                        source_timestamp=scheduled,
                        collection_timestamp=now,
                    ),
                    quality=DataQualityBreakdown(overall=0.9),
                )
            ]

        result, meta = await run_with_retry(
            provider_name=self.name,
            provider_version=self.version,
            settings=cfg,
            fn=_call,
        )
        return result or [], meta


def _raw_quote_to_canonical(raw: RawMarketQuote, now: datetime, provider: str) -> CanonicalQuote:
    return CanonicalQuote(
        as_of=raw.as_of if raw.as_of.tzinfo else raw.as_of.replace(tzinfo=UTC),
        collected_at=now,
        symbol=raw.symbol,
        last=raw.last,
        bid=raw.bid,
        ask=raw.ask,
        previous_close=raw.last / (1 + (raw.gap_pct or 0) / 100.0) if raw.gap_pct else raw.last * 0.998,
        session="fixture",
        spread_bps=spread_bps(raw.bid, raw.ask, raw.last),
        source_ids=[f"{provider}:{raw.symbol}"],
        provenance=Provenance(
            provider_name=provider,
            provider_record_id=raw.symbol,
            raw_payload_reference=f"{provider}:quote:{raw.symbol}",
            source_timestamp=raw.as_of if raw.as_of.tzinfo else raw.as_of.replace(tzinfo=UTC),
            collection_timestamp=now,
            transformations_applied=["raw_to_canonical_quote"],
        ),
        quality=DataQualityBreakdown(overall=0.88, freshness=0.95, completeness=0.9, source_reliability=0.7),
    )


def resolve_market_provider(settings: Settings | None = None) -> Any:
    cfg = settings or get_settings()
    order = [str(x).lower() for x in (list(cfg.market_data_provider_priority) or [cfg.market_data_provider])]
    provider = (cfg.market_data_provider or "").lower()
    if (cfg.broker_provider or "").lower() == "ibkr" and provider in {"auto", ""}:
        provider = "ibkr"
    if cfg.enable_external_data and cfg.enable_market_data_collection:
        if provider == "ibkr" or "ibkr" in order:
            return IbkrMarketDataAdapter()
        if provider == "alpaca" or "alpaca" in order:
            return AlpacaMarketDataAdapter()
    return FixtureMarketDataProvider()


def resolve_news_provider(settings: Settings | None = None) -> Any:
    return FixtureNewsProvider()


def resolve_sec_provider(settings: Settings | None = None) -> Any:
    cfg = settings or get_settings()
    if cfg.enable_external_data and cfg.enable_sec_collection and cfg.sec_provider == "sec_edgar":
        return SecEdgarAdapter()
    return FixtureSecProvider()


def resolve_macro_provider(settings: Settings | None = None) -> Any:
    return FixtureMacroProvider()


def list_providers(settings: Settings | None = None) -> list[dict[str, Any]]:
    cfg = settings or get_settings()
    providers = [
        FixtureMarketDataProvider(),
        IbkrMarketDataAdapter(),
        AlpacaMarketDataAdapter(),
        FixtureNewsProvider(),
        FixtureSecProvider(),
        SecEdgarAdapter(),
        FixtureMacroProvider(),
    ]
    return [
        {
            **p.capabilities().to_dict(),
            "active_for_market": isinstance(resolve_market_provider(cfg), type(p)),
        }
        for p in providers
    ]
