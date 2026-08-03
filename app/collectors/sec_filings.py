"""SEC filings collectors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.collectors.base import RawSecFiling, SecFilingsProvider
from app.core.logging import get_logger

logger = get_logger(__name__)


class StubSecFilingsProvider:
    name = "stub"

    async def fetch_filings(
        self, symbols: list[str], *, since: datetime | None = None
    ) -> list[RawSecFiling]:
        now = datetime.now(UTC)
        filings: list[RawSecFiling] = []
        for symbol in symbols:
            sym = symbol.upper()
            if sym not in {"AAPL", "MSFT", "NVDA"}:
                continue
            filings.append(
                RawSecFiling(
                    symbol=sym,
                    filed_at=now - timedelta(days=5),
                    form_type="8-K",
                    provider=self.name,
                    accession=f"stub-{sym}-8k",
                    title=f"{sym} 8-K material event",
                    url=f"https://example.com/sec/{sym}",
                    summary="Stub filing summary",
                    raw_payload={"stub": True},
                )
            )
        if since:
            filings = [f for f in filings if f.filed_at >= since]
        return filings


def get_sec_filings_provider(name: str | None = None) -> SecFilingsProvider:
    if name and name.lower() not in {"stub", "default"}:
        logger.warning("sec_provider_unknown_fallback_stub", requested=name)
    return StubSecFilingsProvider()
