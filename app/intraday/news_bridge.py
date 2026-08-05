"""Ingest recent high-importance news into the intraday event bus.

Unattended ``evaluate_intraday`` ticks call this so CIO reanalysis can escalate
on news without waiting for a full collection/LLM cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.intraday.events import IntradayEventBus
from app.models import NewsItem

_HIGH_CATEGORIES = {
    "earnings",
    "fed",
    "macro",
    "mna",
    "m&a",
    "guidance",
    "fda",
    "lawsuit",
    "downgrade",
    "upgrade",
}

_HIGH_HEADLINE_TOKENS = (
    "earnings",
    "fed ",
    "fomc",
    "cpi ",
    "inflation",
    "bankruptcy",
    "sec charges",
    "downgrade",
    "guidance cut",
    "acquisition",
    "halted",
)


def classify_news_importance(item: NewsItem) -> str | None:
    """Return high/critical if the row should escalate; else None."""
    payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    raw_imp = str(payload.get("importance") or payload.get("priority") or "").lower()
    if raw_imp in {"critical", "high"}:
        return raw_imp
    cat = (item.category or "").lower().strip()
    if cat in _HIGH_CATEGORIES or any(tok in cat for tok in ("earn", "fed", "macro")):
        return "high"
    headline = (item.headline or "").lower()
    if any(tok in headline for tok in _HIGH_HEADLINE_TOKENS):
        return "high"
    return None


async def ingest_high_importance_news(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    lookback_minutes: int | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    """Publish NEW high-importance news onto the intraday bus (deduped)."""
    cfg = settings or get_settings()
    now = now or datetime.now(UTC)
    lookback = max(15, int(lookback_minutes or cfg.intraday_news_lookback_minutes))
    cutoff = now - timedelta(minutes=lookback)
    bus = IntradayEventBus(session, settings=cfg)

    rows = list(
        (
            await session.execute(
                select(NewsItem)
                .where(NewsItem.published_at >= cutoff)
                .where(NewsItem.is_duplicate.is_(False))
                .order_by(NewsItem.published_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    published: list[str] = []
    skipped = 0
    for item in rows:
        importance = classify_news_importance(item)
        if importance is None:
            skipped += 1
            continue
        event_type = (
            "HIGH_IMPORTANCE_NEWS" if importance in {"high", "critical"} else "EARNINGS_RELEASE"
        )
        dedupe = f"news:{item.provider}:{item.external_id or item.headline_hash}"
        result = await bus.publish(
            event_type=event_type,
            source="news_bridge",
            symbols=[str(s).upper() for s in (item.symbols or []) if s][:12],
            importance=importance,
            deduplication_key=dedupe,
            requires_analysis=True,
            requires_risk_review=importance == "critical",
            payload={
                "headline": (item.headline or "")[:240],
                "news_id": str(item.id),
                "category": item.category,
                "published_at": item.published_at.isoformat() if item.published_at else None,
            },
        )
        if result.status == "NEW":
            published.append(result.event_id)
        else:
            skipped += 1

    return {
        "scanned": len(rows),
        "published": len(published),
        "skipped": skipped,
        "event_ids": published,
        "lookback_minutes": lookback,
    }
