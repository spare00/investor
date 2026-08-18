"""Turn collection records into embeddable text chunks."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from app.embeddings.types import Chunk
from app.services.normalize import (
    NormalizedMacroSnapshot,
    NormalizedMarketSnapshot,
    NormalizedNews,
)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clip(text: str, max_chars: int) -> str:
    t = " ".join((text or "").split())
    if len(t) <= max_chars:
        return t
    return t[: max(0, max_chars - 1)].rstrip() + "…"


def news_chunk(
    item: NormalizedNews,
    *,
    venue: str | None = None,
    max_chars: int = 800,
    as_of: datetime | None = None,
) -> Chunk | None:
    if item.is_duplicate:
        return None
    headline = (item.headline or "").strip()
    if not headline:
        return None
    symbols = [s.upper() for s in (item.symbols or []) if s]
    text = _clip(
        f"NEWS {headline} source={item.source} symbols={','.join(symbols) or '-'}",
        max_chars,
    )
    sid = item.external_id or item.headline_hash or content_hash(headline)
    return Chunk(
        source_type="news",
        source_id=str(sid)[:128],
        text=text,
        content_hash=content_hash(text),
        as_of=as_of or item.published_at or datetime.now(UTC),
        venue=venue,
        symbols=symbols,
        payload={"provider": item.provider, "category": item.category},
    )


def market_chunk(
    snap: NormalizedMarketSnapshot,
    *,
    venue: str | None = None,
    horizon: str | None = None,
    max_chars: int = 800,
) -> Chunk:
    parts = [
        f"MKT {snap.symbol}",
        f"last={snap.last}",
    ]
    if snap.rsi_14 is not None:
        parts.append(f"rsi={snap.rsi_14:.1f}")
    if snap.atr_14 is not None:
        parts.append(f"atr={snap.atr_14:.4f}")
    if snap.sma_20 is not None:
        parts.append(f"sma20={snap.sma_20:.2f}")
    if snap.gap_pct is not None:
        parts.append(f"gap={snap.gap_pct:.2f}%")
    if snap.spread_bps is not None:
        parts.append(f"spread={snap.spread_bps:.1f}bps")
    text = _clip(" ".join(parts), max_chars)
    return Chunk(
        source_type="market",
        source_id=f"{snap.symbol}:{snap.as_of.isoformat()}",
        text=text,
        content_hash=content_hash(text),
        as_of=snap.as_of,
        venue=venue,
        horizon=horizon,
        symbols=[snap.symbol.upper()],
        payload={"provider": snap.provider},
    )


def macro_chunk(
    macro: NormalizedMacroSnapshot,
    *,
    venue: str | None = None,
    max_chars: int = 800,
) -> Chunk:
    notes = " ".join(macro.notes or [])
    text = _clip(
        "MACRO "
        f"ff={macro.fed_funds_rate} cpi={macro.cpi_yoy} pce={macro.pce_yoy} "
        f"unemp={macro.unemployment_rate} dxy={macro.dxy} wti={macro.wti_oil} "
        f"{notes}",
        max_chars,
    )
    return Chunk(
        source_type="macro",
        source_id=macro.as_of.isoformat(),
        text=text,
        content_hash=content_hash(text),
        as_of=macro.as_of,
        venue=venue,
        payload={},
    )


def state_text(
    *,
    venue: str,
    symbols: list[str],
    last_by_symbol: dict[str, float],
    news_hashes: list[str],
    extra: str = "",
) -> str:
    lasts = " ".join(f"{s}={last_by_symbol.get(s, 0):.4f}" for s in symbols)
    news = ",".join(news_hashes[:12])
    return f"STATE venue={venue} px=[{lasts}] news=[{news}] {extra}".strip()


def hits_to_prompt(hits: list[Any], *, max_chars: int = 4000) -> str:
    """Compact retrieved chunks for an agent user prompt (replaces dump_for_prompt)."""
    if not hits:
        return "(no retrieved context)"
    lines: list[str] = []
    used = 0
    for hit in hits:
        chunk = getattr(hit, "chunk", None)
        text = getattr(chunk, "text", None) if chunk is not None else None
        if not text:
            continue
        score = float(getattr(hit, "score", 0.0))
        line = f"- ({score:.2f}) {text}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "Retrieved context:\n" + "\n".join(lines) if lines else "(no retrieved context)"
