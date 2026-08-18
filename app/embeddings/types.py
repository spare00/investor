"""Embedding types for RAG / token reduction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Chunk:
    source_type: str
    source_id: str
    text: str
    content_hash: str
    as_of: datetime
    venue: str | None = None
    horizon: str | None = None
    symbols: list[str] = field(default_factory=list)
    embedding: list[float] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Hit:
    chunk: Chunk
    score: float


@dataclass(slots=True)
class ChangeResult:
    skipped: bool
    cosine: float | None
    reason: str
