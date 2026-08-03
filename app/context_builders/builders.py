"""Agent context builders — canonical data only, untrusted wrappers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.canonical.models import CanonicalNewsItem, FreshnessState
from app.core.config import Settings, get_settings
from app.security.untrusted_text import sanitize_external_text, wrap_untrusted


def _cutoff_filter(items: list[Any], cutoff: datetime | None) -> list[Any]:
    if cutoff is None:
        return items
    out = []
    for item in items:
        ts = getattr(item, "published_at", None) or getattr(item, "as_of", None) or getattr(item, "filed_at", None)
        if ts is None or ts <= cutoff:
            out.append(item)
    return out


class MarketIntelligenceContextBuilder:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def build(
        self,
        *,
        news: list[CanonicalNewsItem],
        filings: list[Any],
        conflicts: list[Any],
        cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        news = _cutoff_filter(news, cutoff)[: self.settings.max_news_context_items]
        filings = _cutoff_filter(filings, cutoff)[: self.settings.max_sec_context_items]
        return {
            "news": [
                {
                    "news_id": n.news_id,
                    "headline": sanitize_external_text(n.headline, max_chars=500),
                    "excerpt": wrap_untrusted(
                        n.source_name,
                        n.body_excerpt or n.summary or "",
                    ),
                    "symbols": n.symbols,
                    "categories": n.categories,
                    "published_at": n.published_at.isoformat(),
                    "source_ids": n.source_ids,
                    "quality": n.quality.to_dict() if n.quality else None,
                    "untrusted": True,
                }
                for n in news
            ],
            "filings": [
                {
                    "accession": getattr(f, "accession_number", None),
                    "form_type": getattr(f, "form_type", None),
                    "symbols": getattr(f, "symbols", []),
                    "filed_at": getattr(f, "filed_at").isoformat()
                    if getattr(f, "filed_at", None)
                    else None,
                    "document_url_reference": getattr(f, "document_url_reference", None),
                    "importance_hints": getattr(f, "importance_hints", []),
                    "source_ids": getattr(f, "source_ids", []),
                    "untrusted": True,
                }
                for f in filings
            ],
            "conflicts": [c.model_dump(mode="json") if hasattr(c, "model_dump") else c for c in conflicts],
            "provider_formats_exposed": False,
        }


class MacroContextBuilder:
    def build(self, *, macro: dict[str, Any], economic_events: list[Any], cutoff: datetime | None = None) -> dict[str, Any]:
        events = _cutoff_filter(economic_events, cutoff)
        return {
            "macro": macro,
            "economic_events": [
                e.model_dump(mode="json") if hasattr(e, "model_dump") else e for e in events
            ],
        }


class QuantContextBuilder:
    def build(self, *, snapshots: list[Any], indicators: dict[str, Any]) -> dict[str, Any]:
        return {
            "snapshots": [
                s.model_dump(mode="json") if hasattr(s, "model_dump") else s for s in snapshots
            ],
            "indicators": indicators,
            "calculation_note": "deterministic_code_only",
        }


class RevalidationContextBuilder:
    def build(
        self,
        *,
        quotes: list[Any],
        premarket: list[Any],
        events: list[dict[str, Any]],
        freshness: dict[str, str],
        conflicts: list[Any],
    ) -> dict[str, Any]:
        return {
            "quotes": [q.model_dump(mode="json") if hasattr(q, "model_dump") else q for q in quotes],
            "premarket": [p.model_dump(mode="json") if hasattr(p, "model_dump") else p for p in premarket],
            "market_events": events,
            "freshness": freshness,
            "conflicts": [c.model_dump(mode="json") if hasattr(c, "model_dump") else c for c in conflicts],
            "stale_explicit": [k for k, v in freshness.items() if v in {FreshnessState.STALE.value, FreshnessState.EXPIRED.value}],
        }


class IntradayContextBuilder:
    def build(self, *, events: list[dict[str, Any]], quotes: list[Any]) -> dict[str, Any]:
        return {
            "events": events,
            "quotes": [q.model_dump(mode="json") if hasattr(q, "model_dump") else q for q in quotes],
        }
