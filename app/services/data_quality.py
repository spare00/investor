"""Data quality helpers used by collectors and risk gates."""

from __future__ import annotations

import re
from datetime import UTC, datetime


_WHITESPACE = re.compile(r"\s+")


def normalize_headline(headline: str) -> str:
    return _WHITESPACE.sub(" ", headline.strip().lower())


def dedupe_news_headlines(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop near-duplicate headlines (case/whitespace insensitive)."""
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for item in items:
        key = normalize_headline(str(item.get("headline", "")))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def is_fresh(
    ts: datetime,
    *,
    now: datetime | None = None,
    max_age_minutes: int = 60,
) -> bool:
    current = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = current - ts
    return age.total_seconds() <= max_age_minutes * 60


def score_freshness(
    ts: datetime,
    *,
    now: datetime | None = None,
    max_age_minutes: int = 60,
) -> float:
    """Linear decay from 1.0 at age=0 to 0.0 at max_age (clamped)."""
    current = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_minutes = max(0.0, (current - ts).total_seconds() / 60.0)
    if max_age_minutes <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (age_minutes / max_age_minutes)))
