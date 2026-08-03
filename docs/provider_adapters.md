# Provider Adapters

## Fixture (default)

| Type | Name | Notes |
|------|------|-------|
| Market | `fixture` | Deterministic quotes/bars/premarket |
| News | `fixture` | Synthetic headlines |
| SEC | `fixture` | Synthetic 8-K metadata |
| Macro / calendar | `fixture` | Macro snapshot + scheduled CPI |

## Real (opt-in)

| Adapter | Requires | Status |
|---------|----------|--------|
| `alpaca` market quotes | `ENABLE_EXTERNAL_DATA` + `ENABLE_MARKET_DATA_COLLECTION` + Alpaca keys | Implemented (latest quotes HTTP) |
| `sec_edgar` | `ENABLE_EXTERNAL_DATA` + `ENABLE_SEC_COLLECTION` + SEC User-Agent | Implemented (tickers + recent filings metadata) |

Paid news/macro vendors are **not** wired; do not assume free live news.

Common behavior: timeout, retry, circuit breaker, credential redaction (`app/providers/base.py`).
