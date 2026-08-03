"""Earnings event collectors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.collectors.base import EarningsProvider, RawEarningsEvent
from app.core.logging import get_logger

logger = get_logger(__name__)


class StubEarningsProvider:
    name = "stub"

    async def fetch_earnings(
        self, symbols: list[str], *, since: datetime | None = None
    ) -> list[RawEarningsEvent]:
        now = datetime.now(UTC)
        events: list[RawEarningsEvent] = []
        for symbol in symbols:
            sym = symbol.upper()
            if sym not in {"NVDA", "MSFT", "AAPL", "META"}:
                continue
            events.append(
                RawEarningsEvent(
                    symbol=sym,
                    report_date=now - timedelta(days=2),
                    provider=self.name,
                    period="Q2",
                    eps_actual=1.25,
                    eps_estimate=1.10,
                    revenue_actual=30_000_000_000,
                    revenue_estimate=28_500_000_000,
                    guidance_summary="Inline guidance",
                    raw_payload={"stub": True},
                )
            )
        if since:
            events = [e for e in events if e.report_date >= since]
        return events


def get_earnings_provider(name: str | None = None) -> EarningsProvider:
    if name and name.lower() not in {"stub", "default"}:
        logger.warning("earnings_provider_unknown_fallback_stub", requested=name)
    return StubEarningsProvider()
