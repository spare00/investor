"""News deduplication and event clustering (deterministic)."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from app.canonical.models import CanonicalNewsEventCluster, CanonicalNewsItem, Provenance
from app.services.data_quality import normalize_headline


def _norm_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.strip().lower().rstrip("/")


def _fingerprint(item: CanonicalNewsItem) -> str:
    symbols = ",".join(sorted(s.upper() for s in item.symbols))
    cats = ",".join(sorted(item.categories))
    base = f"{symbols}|{cats}|{normalize_headline(item.headline)[:80]}"
    return hashlib.sha256(base.encode()).hexdigest()[:24]


def cluster_news(
    items: list[CanonicalNewsItem],
    *,
    window: timedelta = timedelta(hours=6),
) -> tuple[list[CanonicalNewsItem], list[CanonicalNewsEventCluster]]:
    """Deduplicate and cluster; returns unique items + clusters."""
    by_provider: dict[str, CanonicalNewsItem] = {}
    by_url: dict[str, CanonicalNewsItem] = {}
    by_headline: dict[str, CanonicalNewsItem] = {}
    unique: list[CanonicalNewsItem] = []
    clusters: dict[str, CanonicalNewsEventCluster] = {}
    now = datetime.now(UTC)

    for item in items:
        pid = item.provider_article_id
        if pid and pid in by_provider:
            item = _mark_dup(item, "provider_article_id")
            _add_to_cluster(clusters, by_provider[pid], item, "provider_id", now)
            continue
        url = _norm_url(item.source_url_reference)
        if url and url in by_url:
            item = _mark_dup(item, "url")
            _add_to_cluster(clusters, by_url[url], item, "url", now)
            continue
        hh = normalize_headline(item.headline)
        if hh in by_headline:
            primary = by_headline[hh]
            if abs((item.published_at - primary.published_at).total_seconds()) <= window.total_seconds():
                item = _mark_dup(item, "headline_hash")
                _add_to_cluster(clusters, primary, item, "headline_hash", now)
                continue
        # Similar headline within window + same symbols
        matched = None
        for other in unique:
            if set(s.upper() for s in other.symbols) & set(s.upper() for s in item.symbols):
                if _similar(other.headline, item.headline) and abs(
                    (item.published_at - other.published_at).total_seconds()
                ) <= window.total_seconds():
                    matched = other
                    break
        if matched is not None:
            item = _mark_dup(item, "similar_headline")
            _add_to_cluster(clusters, matched, item, "similar_headline", now)
            continue

        unique.append(item)
        if pid:
            by_provider[pid] = item
        if url:
            by_url[url] = item
        by_headline[hh] = item
        fp = _fingerprint(item)
        clusters[fp] = CanonicalNewsEventCluster(
            as_of=item.published_at,
            collected_at=item.collected_at,
            event_cluster_id=fp,
            primary_article_id=item.news_id,
            member_article_ids=[item.news_id],
            first_seen_at=item.published_at,
            last_updated_at=item.updated_at or item.published_at,
            affected_symbols=list(item.symbols),
            category=item.categories[0] if item.categories else None,
            confidence=1.0,
            deduplication_method="primary",
            provenance=Provenance(
                provider_name="news_cluster",
                provider_record_id=fp,
                collection_timestamp=now,
                source_timestamp=item.published_at,
            ),
        )
    return unique, list(clusters.values())


def _mark_dup(item: CanonicalNewsItem, method: str) -> CanonicalNewsItem:
    data = item.model_dump()
    q = data.get("quality") or {}
    issues = list(q.get("issues") or [])
    issues.append(f"duplicate:{method}")
    q["issues"] = issues
    data["quality"] = q
    return CanonicalNewsItem.model_validate(data)


def _add_to_cluster(
    clusters: dict[str, CanonicalNewsEventCluster],
    primary: CanonicalNewsItem,
    member: CanonicalNewsItem,
    method: str,
    now: datetime,
) -> None:
    fp = _fingerprint(primary)
    cluster = clusters.get(fp)
    if cluster is None:
        return
    if member.news_id not in cluster.member_article_ids:
        cluster.member_article_ids.append(member.news_id)
    cluster.last_updated_at = max(cluster.last_updated_at, member.published_at)
    cluster.deduplication_method = method
    cluster.confidence = min(1.0, cluster.confidence)
    if member.is_correction:
        cluster.confidence = min(cluster.confidence, 0.9)


def _similar(a: str, b: str) -> bool:
    na = set(re.findall(r"[a-z0-9]+", normalize_headline(a)))
    nb = set(re.findall(r"[a-z0-9]+", normalize_headline(b)))
    if not na or not nb:
        return False
    inter = len(na & nb)
    union = len(na | nb)
    return (inter / union) >= 0.6
