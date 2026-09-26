"""Modeled trading costs.

Lives in ``core`` so both the entry gate (``app.universe.book_strategy``) and
the expectancy report (``app.performance.entry_expectancy``) can price a trade
the same way without importing each other.
"""

from __future__ import annotations

# Settlement stamps fees=0. 8 bps round-trip is a conservative liquid-name
# paper cost (≈4 bps each way) so expectancy is not gross-only.
DEFAULT_ROUND_TRIP_COST_BPS = 8.0
