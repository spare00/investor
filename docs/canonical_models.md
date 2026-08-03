# Canonical Models

Pydantic models in `app/canonical/models.py`:

CanonicalQuote, CanonicalBar, CanonicalPremarketSnapshot, CanonicalNewsItem,
CanonicalNewsEventCluster, CanonicalSecFiling, CanonicalEconomicEvent,
CanonicalMarketSnapshot, DataQualityBreakdown, Provenance, CanonicalDataConflict.

Timestamps are timezone-aware UTC. Premarket availability is explicit
(`AVAILABLE|PARTIAL|UNSUPPORTED|STALE|MISSING|CONFLICTED`) — never silently filled from regular session.
