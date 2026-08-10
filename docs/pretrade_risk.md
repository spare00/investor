# Pre-trade Risk Validator

Deterministic checks in `app/execution/pretrade.py` run before approval and again conceptually before submit. LLM output cannot replace this gate.

Checks include: emergency stop, pause, hard vetoes (incl. `currency_mismatch` when trade currency ≠ portfolio base), market session, asset tradability, decision expiry, data quality, spread, stop required, position/venue sizing caps, shorting disabled, duplicate/conflict flags.

Results: `APPROVED`, `APPROVED_WITH_REDUCTION`, `REQUIRES_MANUAL_APPROVAL`, `REJECTED`, `SYSTEM_BLOCKED`.

Sizing: `app/execution/sizing.py` — risk amount ÷ stop distance, then position/BP/liquidity caps.
