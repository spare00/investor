"""Embedding RAG foundation — local hash embedder, retrieve, skip-gate."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.embeddings.chunks import hits_to_prompt, market_chunk, news_chunk, state_text
from app.embeddings.client import HashEmbedder, get_embedder
from app.embeddings.retrieve import source_types_for
from app.embeddings.service import EmbeddingService
from app.embeddings.store import MemoryVectorStore, cosine
from app.services.collection import CollectionBundle
from app.services.normalize import NormalizedMacroSnapshot, NormalizedMarketSnapshot, NormalizedNews


def _news(headline: str, symbols: list[str], *, dup: bool = False) -> NormalizedNews:
    now = datetime.now(UTC)
    return NormalizedNews(
        provider="fixture",
        external_id=headline[:20],
        headline=headline,
        headline_hash=headline,
        source="test",
        url=None,
        published_at=now,
        collected_at=now,
        symbols=symbols,
        category="earnings",
        raw_payload={},
        freshness_score=1.0,
        quality_score=1.0,
        is_duplicate=dup,
    )


def _mkt(symbol: str, last: float) -> NormalizedMarketSnapshot:
    return NormalizedMarketSnapshot(
        symbol=symbol,
        as_of=datetime.now(UTC),
        provider="fixture",
        last=last,
        open=last - 1,
        high=last + 1,
        low=last - 2,
        volume=1_000_000,
        avg_volume_20d=2_000_000,
        atr_14=1.5,
        rsi_14=55.0,
        sma_20=last - 0.5,
        sma_50=None,
        sma_200=None,
        bid=last - 0.01,
        ask=last + 0.01,
        spread_bps=4.0,
        premarket_change_pct=0.2,
        gap_pct=0.1,
        vix=16.0,
        raw_payload={},
        freshness_score=1.0,
        quality_score=1.0,
    )


def _macro() -> NormalizedMacroSnapshot:
    return NormalizedMacroSnapshot(
        as_of=datetime.now(UTC),
        provider="fixture",
        fed_funds_rate=4.5,
        cpi_yoy=2.8,
        pce_yoy=2.6,
        unemployment_rate=4.1,
        gdp_growth_q_o_q=0.5,
        us_10y_yield=4.2,
        us_2y_yield=4.0,
        dxy=104.0,
        wti_oil=78.0,
        gold=2300.0,
        hy_credit_spread_bps=320.0,
        notes=["soft landing"],
        raw_payload={},
        freshness_score=1.0,
        quality_score=1.0,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        enable_embeddings=True,
        embedding_provider="hash",
        embedding_hash_dim=64,
        embedding_top_k=5,
        embedding_skip_reanalysis_cosine=0.97,
        llm_api_key=None,
    )


async def test_hash_embedder_identical_texts_match() -> None:
    emb = HashEmbedder(dim=32)
    a, b = await emb.embed(["NVDA earnings beat guidance", "NVDA earnings beat guidance"])
    assert cosine(a, b) == pytest.approx(1.0)


async def test_hash_embedder_related_beats_unrelated() -> None:
    emb = HashEmbedder(dim=64)
    a, b, c = await emb.embed(
        [
            "NVDA earnings beat AI demand",
            "NVDA earnings beat data center",
            "wheat harvest drought australia",
        ]
    )
    assert cosine(a, b) > cosine(a, c)


def test_source_types_scalp_cio_drops_watchlist_macro() -> None:
    types = source_types_for(agent="cio", horizon="scalp")
    assert "news" in types
    assert "market" in types
    assert "watchlist" not in types


def test_source_types_medium_prefers_macro() -> None:
    types = source_types_for(agent="macro_strategist", horizon="medium")
    assert types == ["macro"]


def test_news_chunk_skips_duplicates() -> None:
    assert news_chunk(_news("dup", ["NVDA"], dup=True)) is None
    chunk = news_chunk(_news("NVDA beats", ["NVDA"]))
    assert chunk is not None
    assert chunk.source_type == "news"
    assert "NVDA" in chunk.symbols


async def test_index_and_retrieve_news_for_mi(settings: Settings) -> None:
    svc = EmbeddingService(settings=settings, embedder=HashEmbedder(64), store=MemoryVectorStore())
    bundle = CollectionBundle(
        workflow_id=uuid4(),
        collected_at=datetime.now(UTC),
        news=[
            _news("NVDA reports record data center revenue", ["NVDA"]),
            _news("BHP iron ore shipment delay", ["BHP"]),
        ],
        markets=[_mkt("NVDA", 120.0), _mkt("BHP", 45.0)],
        macro=_macro(),
    )
    n = await svc.index_collection(bundle, venue="US", horizon_by_symbol={"NVDA": "day"})
    assert n >= 3
    hits = await svc.retrieve(
        agent="market_intelligence", horizon="day", symbols=["NVDA"], venue="US"
    )
    assert hits
    assert any("NVDA" in h.chunk.text for h in hits)
    prompt = svc.prompt_context(hits)
    assert prompt.startswith("Retrieved context:")
    assert len(prompt) < 4000


async def test_skip_reanalysis_when_state_unchanged(settings: Settings) -> None:
    svc = EmbeddingService(settings=settings, embedder=HashEmbedder(64), store=MemoryVectorStore())
    text = state_text(
        venue="US",
        symbols=["SPY", "QQQ"],
        last_by_symbol={"SPY": 500.1, "QQQ": 430.2},
        news_hashes=["aaa", "bbb"],
    )
    first = await svc.observe_state("US:2026-08-18", text)
    assert first.skipped is False
    second = await svc.observe_state("US:2026-08-18", text)
    assert second.skipped is True
    assert second.cosine is not None and second.cosine >= 0.97


async def test_do_not_skip_when_state_moves(settings: Settings) -> None:
    svc = EmbeddingService(settings=settings, embedder=HashEmbedder(64), store=MemoryVectorStore())
    await svc.observe_state(
        "US:session",
        state_text(
            venue="US",
            symbols=["SPY"],
            last_by_symbol={"SPY": 500.0},
            news_hashes=["old"],
        ),
    )
    moved = await svc.observe_state(
        "US:session",
        state_text(
            venue="US",
            symbols=["SPY"],
            last_by_symbol={"SPY": 512.0},
            news_hashes=["new-catalyst"],
            extra="HIGH_IMPORTANCE_NEWS NVDA",
        ),
    )
    assert moved.skipped is False


def test_get_embedder_defaults_to_hash() -> None:
    settings = Settings(embedding_provider="openai", llm_api_key=None, enable_embeddings=True)
    emb = get_embedder(settings)
    assert isinstance(emb, HashEmbedder)


def test_hits_to_prompt_empty() -> None:
    assert hits_to_prompt([]) == "(no retrieved context)"


def test_market_chunk_includes_symbol() -> None:
    chunk = market_chunk(_mkt("QQQ", 430.0), venue="US", horizon="scalp")
    assert chunk.source_type == "market"
    assert chunk.horizon == "scalp"
    assert "QQQ" in chunk.text
