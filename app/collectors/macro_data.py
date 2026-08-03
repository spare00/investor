"""Macro / rates / commodities collectors."""

from __future__ import annotations

from datetime import UTC, datetime

from app.collectors.base import MacroDataProvider, RawMacroSnapshot
from app.core.logging import get_logger

logger = get_logger(__name__)


class StubMacroDataProvider:
    name = "stub"

    async def fetch_macro(self) -> RawMacroSnapshot:
        return RawMacroSnapshot(
            as_of=datetime.now(UTC),
            provider=self.name,
            fed_funds_rate=5.25,
            cpi_yoy=2.9,
            pce_yoy=2.6,
            unemployment_rate=4.1,
            gdp_growth_q_o_q=2.1,
            us_10y_yield=4.25,
            us_2y_yield=4.05,
            dxy=104.2,
            wti_oil=78.5,
            gold=2350.0,
            hy_credit_spread_bps=320.0,
            notes=["Stub macro snapshot for offline workflows"],
            raw_payload={"stub": True},
        )


def get_macro_data_provider(name: str | None = None) -> MacroDataProvider:
    # Only stub is implemented in Phase 2.
    if name and name.lower() not in {"stub", "default"}:
        logger.warning("macro_provider_unknown_fallback_stub", requested=name)
    return StubMacroDataProvider()
