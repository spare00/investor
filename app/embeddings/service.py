"""Index collection chunks and retrieve compact context. Pipeline wiring is a later step."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.embeddings.chunks import (
    content_hash,
    hits_to_prompt,
    macro_chunk,
    market_chunk,
    news_chunk,
)
from app.embeddings.client import Embedder, get_embedder
from app.embeddings.retrieve import query_text, source_types_for
from app.embeddings.store import MemoryVectorStore, cosine
from app.embeddings.types import ChangeResult, Chunk, Hit
from app.services.collection import CollectionBundle


class EmbeddingService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        embedder: Embedder | None = None,
        store: MemoryVectorStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or get_embedder(self.settings)
        self.store = store or MemoryVectorStore()

    async def index_collection(
        self,
        bundle: CollectionBundle,
        *,
        venue: str | None = None,
        horizon_by_symbol: dict[str, str] | None = None,
    ) -> int:
        """Embed new/changed news + market + macro chunks. Returns upsert count."""
        max_chars = int(self.settings.embedding_max_chunk_chars)
        horizons = {k.upper(): v for k, v in (horizon_by_symbol or {}).items()}
        pending: list[Chunk] = []
        for item in bundle.news:
            chunk = news_chunk(item, venue=venue, max_chars=max_chars)
            if chunk is not None:
                pending.append(chunk)
        for snap in bundle.markets:
            hz = horizons.get(snap.symbol.upper())
            pending.append(
                market_chunk(snap, venue=venue, horizon=hz, max_chars=max_chars)
            )
        if bundle.macro is not None:
            pending.append(macro_chunk(bundle.macro, venue=venue, max_chars=max_chars))
        return await self.index_chunks(pending)

    async def index_chunks(self, chunks: list[Chunk]) -> int:
        to_embed: list[Chunk] = []
        for chunk in chunks:
            existing = self.store.get(chunk.source_type, chunk.source_id)
            if existing is not None and existing.content_hash == chunk.content_hash:
                continue
            to_embed.append(chunk)
        if not to_embed:
            return 0
        vectors = await self.embedder.embed([c.text for c in to_embed])
        n = 0
        for chunk, vec in zip(to_embed, vectors, strict=True):
            chunk.embedding = vec
            self.store.upsert(chunk)
            n += 1
        return n

    async def retrieve(
        self,
        *,
        agent: str | None = None,
        horizon: str | None = None,
        venue: str | None = None,
        symbols: list[str] | None = None,
        extra_query: str = "",
        top_k: int | None = None,
    ) -> list[Hit]:
        k = int(top_k if top_k is not None else self.settings.embedding_top_k)
        q = query_text(agent=agent, symbols=symbols or [], extra=extra_query)
        vectors = await self.embedder.embed([q])
        types = source_types_for(agent=agent, horizon=horizon)
        return self.store.query(
            vectors[0],
            top_k=k,
            source_types=types,
            venue=venue,
            symbols=symbols,
        )

    def prompt_context(self, hits: list[Hit]) -> str:
        return hits_to_prompt(hits, max_chars=int(self.settings.embedding_prompt_max_chars))

    async def observe_state(self, key: str, text: str) -> ChangeResult:
        """Compare a compact session-state string to the last indexed one.

        High cosine → skip another 6-agent LLM pass (the main token leak).
        """
        threshold = float(self.settings.embedding_skip_reanalysis_cosine)
        vectors = await self.embedder.embed([text])
        vec = vectors[0]
        prior = self.store.get("state", key)
        score = cosine(vec, prior.embedding) if prior is not None else None
        chunk = Chunk(
            source_type="state",
            source_id=key,
            text=text,
            content_hash=content_hash(text),
            as_of=datetime.now(UTC),
            embedding=vec,
        )
        self.store.upsert(chunk)
        if score is None:
            return ChangeResult(skipped=False, cosine=None, reason="no_prior_state")
        if score >= threshold:
            return ChangeResult(
                skipped=True, cosine=score, reason="state_unchanged"
            )
        return ChangeResult(skipped=False, cosine=score, reason="state_changed")
