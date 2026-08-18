"""Embedding / RAG helpers. Off by default; not yet wired into AgentPipeline."""

from app.embeddings.client import HashEmbedder, get_embedder
from app.embeddings.service import EmbeddingService
from app.embeddings.store import MemoryVectorStore

__all__ = [
    "EmbeddingService",
    "HashEmbedder",
    "MemoryVectorStore",
    "get_embedder",
]
