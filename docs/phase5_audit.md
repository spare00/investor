# Phase 5 Audit (pre Phase 6)

Date: 2026-08-04  
Command: `pytest tests/ -q` → **148 passed, 1 skipped** (Alpaca smoke opt-in)

## Implemented Correctly

- Broker interface (`BrokerClient` Protocol) isolated; Agents never import brokers
- MockBroker supports submit/partial/fill/cancel/replace/timeout/failure/idempotency
- Alpaca adapter refuses non-paper URL and `ENABLE_LIVE_TRADING`
- Factory hard-blocks live environment before constructing Alpaca
- CIO Decision → Order Intent separation via `ExecutionService`
- Manual approval path blocks submit until `APPROVED`
- `client_order_id` / `idempotency_key` unique in DB; ExecutionService timeout → UNKNOWN + client-id lookup
- Emergency Stop blocks new orders and cancels opens (positions not closed by default)
- Credentials are `SecretStr`; Alpaca errors redacted; not passed into Agent Context
- Defaults: mock provider, orders/connection/automation off, manual approval on, live false

## Partially Implemented

- PretradeRiskValidator only on ExecutionService path; Workflow auto-submit uses ExecutionValidator + OrderManager (defaults keep auto-submit unreachable)
- Final submit revalidation mostly age warning, not full Pretrade re-run (corrected before Phase 6)
- Reconciliation detects open-order ID drift but `blocks_new_orders` was advisory-only (corrected)
- State machine defined; broker sync updates status without always calling `assert_order_transition`
- Exit policy stored on intent; protection orders not submitted (`protection_submitted=false`)

## Missing (filled in Phase 6)

- Event bus with dedup/cooldown
- Position Lifecycle distinct from broker mirror + snapshots
- Position Monitor and dynamic risk revalidation
- Stop/take-profit/invalidation state machines
- Closing Policy / Overnight Review execution
- Postmarket settlement, deterministic PnL, post-trade review
- Intraday CIO decision model and recovery orchestration

## Incorrect or Risky

- Dual execution pipelines (ExecutionService vs OrderManager) with divergent safety — keep automation defaults off; Phase 6 routes exits through Intent path
- OrderManager did not catch TimeoutError (corrected)
- PortfolioSnapshot stored raw account id (corrected to redact)

## Intraday Integration Risks

- Premarket-style full agent re-run is expensive; Phase 6 adds event-driven evaluate with rate limits
- Reconciliation was on-demand only — Phase 6 recovery/poll integrates it

## Position Management Risks

- Position delete/recreate wiped lifecycle fields — Phase 6 adds `position_lifecycles` + snapshots not destroyed by broker sync

## Security Concerns

- Account id at rest in snapshots — redacted
- Dead pretrade params (`account_blocked` etc.) — wired where available in Phase 6 submit path

## Compatibility Concerns

- `SimulatedBroker` shim retained for older tests; prefer `MockBroker`
- Two pretrade concepts (PretradeRiskValidator vs DeterministicRiskEngine) — document; Intent path is canonical for Phase 6

## Tests Executed

```bash
pytest tests/ -q
# 148 passed, 1 skipped
```

## Corrections Applied

- Re-run hard pretrade gates on final submit
- Material reconciliation drift blocks new submits
- OrderManager catches TimeoutError → reconciliation-required
- Redact account id in portfolio snapshot payload
- Introduce Phase 6 position lifecycle tables (not wiped by broker sync)
