"""Embedding clients: local hashing trick (tests/offline) and OpenAI embeddings."""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic bag-of-tokens hashing trick. No network. For tests and offline."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = max(8, int(dim))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hash_vector(text, self.dim) for text in texts]


def _hash_vector(text: str, dim: int) -> list[float]:
    vec = np.zeros(dim, dtype=np.float64)
    for tok in (text or "").lower().split():
        digest = hashlib.md5(tok.encode("utf-8"), usedforsecurity=False).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = float(np.linalg.norm(vec)) or 1.0
    return (vec / norm).tolist()


class OpenAIEmbedder:
    """OpenAI-compatible POST /embeddings. Not charged against chat LLM budget."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.dim = 1536 if "small" in (self.settings.embedding_model or "") else 3072

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        cfg = self.settings
        api_key = cfg.llm_api_key.get_secret_value() if cfg.llm_api_key else ""
        if not api_key.strip():
            raise RuntimeError("embedding_openai_missing_api_key")
        cleaned = [t if t.strip() else " " for t in texts]
        payload = {"model": cfg.embedding_model, "input": cleaned}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = cfg.llm_base_url.rstrip("/") + "/embeddings"
        timeout = httpx.Timeout(cfg.llm_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                logger.error(
                    "embedding_http_error",
                    status=response.status_code,
                    body=response.text[:400],
                )
                raise RuntimeError(f"embedding HTTP {response.status_code}")
            data = response.json()
        items = sorted(data.get("data") or [], key=lambda row: int(row.get("index", 0)))
        vectors = [list(map(float, row.get("embedding") or [])) for row in items]
        if len(vectors) != len(cleaned):
            raise RuntimeError("embedding_count_mismatch")
        if vectors and vectors[0]:
            self.dim = len(vectors[0])
        return vectors


def get_embedder(settings: Settings | None = None) -> Embedder:
    cfg = settings or get_settings()
    provider = (cfg.embedding_provider or "hash").strip().lower()
    if provider == "openai":
        key = cfg.llm_api_key.get_secret_value().strip() if cfg.llm_api_key else ""
        if key:
            return OpenAIEmbedder(cfg)
        logger.warning("embedding_fallback_hash", reason="missing_api_key")
    return HashEmbedder(dim=int(cfg.embedding_hash_dim))
