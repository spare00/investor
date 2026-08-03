"""Deterministic high-importance market event generation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.canonical.models import (
    CanonicalEconomicEvent,
    CanonicalNewsItem,
    CanonicalPremarketSnapshot,
    CanonicalSecFiling,
    ConflictState,
    PremarketAvailability,
)


def build_market_events(
    *,
    news: list[CanonicalNewsItem],
    filings: list[CanonicalSecFiling],
    economic: list[CanonicalEconomicEvent],
    premarket: list[CanonicalPremarketSnapshot],
    conflicts: list[Any],
    stale_symbols: list[str],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    events: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(ev: dict[str, Any]) -> None:
        key = ev["deduplication_key"]
        if key in seen:
            return
        seen.add(key)
        events.append(ev)

    for item in news:
        if item.importance in {"high", "critical"} or "earnings" in item.categories:
            _add(
                {
                    "event_id": str(uuid4()),
                    "event_type": "HIGH_IMPORTANCE_NEWS"
                    if item.importance in {"high", "critical"}
                    else "EARNINGS_RELEASE",
                    "importance": item.importance,
                    "detected_at": now.isoformat(),
                    "effective_at": item.published_at.isoformat(),
                    "symbols": item.symbols,
                    "sectors": item.sectors,
                    "source_record_ids": [item.news_id],
                    "trigger_reason": item.headline[:200],
                    "requires_reanalysis": True,
                    "requires_risk_review": item.importance == "critical",
                    "deduplication_key": f"news:{item.news_id}",
                    "expires_at": (now + timedelta(hours=6)).isoformat(),
                }
            )

    for f in filings:
        if f.form_type in {"8-K", "S-1"} or "earnings" in f.importance_hints:
            _add(
                {
                    "event_id": str(uuid4()),
                    "event_type": "SEC_MATERIAL_FILING",
                    "importance": "high",
                    "detected_at": now.isoformat(),
                    "effective_at": f.filed_at.isoformat(),
                    "symbols": f.symbols,
                    "sectors": [],
                    "source_record_ids": [f.accession_number],
                    "trigger_reason": f"{f.form_type} {f.company_name}",
                    "requires_reanalysis": True,
                    "requires_risk_review": True,
                    "deduplication_key": f"sec:{f.accession_number}",
                    "expires_at": (now + timedelta(hours=24)).isoformat(),
                }
            )

    for e in economic:
        if e.importance == "high":
            _add(
                {
                    "event_id": str(uuid4()),
                    "event_type": "FED_EVENT" if "fed" in e.event_name.lower() else "ECONOMIC_RELEASE",
                    "importance": "high",
                    "detected_at": now.isoformat(),
                    "effective_at": (e.released_at or e.scheduled_at).isoformat(),
                    "symbols": ["SPY", "QQQ", "IWM", "DIA"],
                    "sectors": [],
                    "source_record_ids": [e.event_id],
                    "trigger_reason": e.event_name,
                    "requires_reanalysis": True,
                    "requires_risk_review": True,
                    "deduplication_key": f"econ:{e.event_id}:{e.status.value}",
                    "expires_at": (now + timedelta(hours=4)).isoformat(),
                }
            )

    for p in premarket:
        gap = p.gap_from_previous_close_pct
        if gap is not None and abs(gap) >= 1.5 and p.availability == PremarketAvailability.AVAILABLE:
            _add(
                {
                    "event_id": str(uuid4()),
                    "event_type": "PRICE_GAP",
                    "importance": "medium",
                    "detected_at": now.isoformat(),
                    "effective_at": p.as_of.isoformat(),
                    "symbols": [p.symbol],
                    "sectors": [],
                    "source_record_ids": [str(p.id)],
                    "trigger_reason": f"premarket_gap={gap:.2f}%",
                    "requires_reanalysis": abs(gap) >= 3.0,
                    "requires_risk_review": abs(gap) >= 5.0,
                    "deduplication_key": f"gap:{p.symbol}:{p.as_of.date().isoformat()}",
                    "expires_at": (now + timedelta(hours=6)).isoformat(),
                }
            )

    for c in conflicts:
        state = getattr(c, "state", None)
        if state == ConflictState.MATERIAL_CONFLICT:
            _add(
                {
                    "event_id": str(uuid4()),
                    "event_type": "DATA_CONFLICT",
                    "importance": "high",
                    "detected_at": now.isoformat(),
                    "effective_at": now.isoformat(),
                    "symbols": [getattr(c, "symbol_or_key", "")],
                    "sectors": [],
                    "source_record_ids": [str(getattr(c, "id", ""))],
                    "trigger_reason": "material_provider_conflict",
                    "requires_reanalysis": False,
                    "requires_risk_review": True,
                    "deduplication_key": f"conflict:{getattr(c, 'symbol_or_key', '')}:{getattr(c, 'data_type', '')}",
                    "expires_at": (now + timedelta(hours=2)).isoformat(),
                }
            )

    for sym in stale_symbols:
        _add(
            {
                "event_id": str(uuid4()),
                "event_type": "DATA_STALE",
                "importance": "high",
                "detected_at": now.isoformat(),
                "effective_at": now.isoformat(),
                "symbols": [sym],
                "sectors": [],
                "source_record_ids": [],
                "trigger_reason": "quote_expired",
                "requires_reanalysis": False,
                "requires_risk_review": True,
                "deduplication_key": f"stale:{sym}:{now.date().isoformat()}",
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }
        )

    return events
