# Pre-trade Risk Validator

Deterministic checks in `app/execution/pretrade.py` run before approval and again conceptually before submit. LLM output cannot replace this gate.

Checks include: emergency stop, pause, hard vetoes (incl. `currency_mismatch` when trade currency ≠ portfolio base and no `FX_RATES` pair), market session, asset tradability, decision expiry, data quality, spread, stop required, position/venue sizing caps, shorting disabled, duplicate/conflict flags.

Static FX: set `FX_RATES=AUDUSD:0.65` so AUD-priced ASX entries can size against a USD base book (entry/stop converted to base). Empty `FX_RATES` keeps fail-closed behaviour.

Results: `APPROVED`, `APPROVED_WITH_REDUCTION`, `REQUIRES_MANUAL_APPROVAL`, `REJECTED`, `SYSTEM_BLOCKED`.

Sizing: `app/execution/sizing.py` — risk amount ÷ stop distance, then position/BP/liquidity caps.
