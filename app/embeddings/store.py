"""In-memory cosine store. SQL persistence is the EmbeddingChunk table (next wiring)."""

from __future__ import annotations

import math

from app.embeddings.types import Chunk, Hit


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class MemoryVectorStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Chunk] = {}

    def upsert(self, chunk: Chunk) -> None:
        self._rows[(chunk.source_type, chunk.source_id)] = chunk

    def get(self, source_type: str, source_id: str) -> Chunk | None:
        return self._rows.get((source_type, source_id))

    def query(
        self,
        vector: list[float],
        *,
        top_k: int = 8,
        source_types: list[str] | None = None,
        venue: str | None = None,
        symbols: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[Hit]:
        allow_types = {t.lower() for t in source_types} if source_types else None
        want_syms = {s.upper() for s in symbols} if symbols else None
        venue_u = venue.upper() if venue else None
        hits: list[Hit] = []
        for chunk in self._rows.values():
            if allow_types is not None and chunk.source_type.lower() not in allow_types:
                continue
            if venue_u and chunk.venue and chunk.venue.upper() != venue_u:
                continue
            if want_syms and chunk.symbols:
                if not want_syms.intersection({s.upper() for s in chunk.symbols}):
                    continue
            score = cosine(vector, chunk.embedding)
            if score < min_score:
                continue
            hits.append(Hit(chunk=chunk, score=score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: max(1, int(top_k))]
