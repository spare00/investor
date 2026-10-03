# Phase 4 Audit (pre Phase 5)

Date: 2026-08-04  
Command: `pytest tests/ -q` → **135 passed**

## Implemented Correctly

- Provider adapters isolated from Agents (`app/providers/`); agents consume Context Builders / `CollectionBundle`
- Fixture providers return Canonical models; IBKR quotes and SEC EDGAR are opt-in behind flags
- Market/news/SEC/economic canonical models with provenance
- News dedup + clustering; untrusted text wrappers for prompt-injection defense
- Premarket analysis uses `DataCollectionPipeline` → legacy bundle → 6-agent chain
- Scheduler calls only `DailyWorkflowService` (not providers/LLM/broker)
- Defaults: `ENABLE_EXTERNAL_DATA=false`, `ENABLE_BROKER_ORDERS=false`
- Fixture mode works without external credentials

## Partially Implemented

- Quality/freshness stored on canonical records and in collection `quality_summary`; Context includes quality dicts but Agents do not yet weight every field in prompts
- Material conflicts mostly `SINGLE_SOURCE_ONLY` in fixture mode (secondary providers rarely co-called)
- Collection run persistence lean (API in-memory cache + migration tables; not all writes go through DB on every collect)
- IBKR market adapter covers latest quotes; full OHLCV history is limited

## Missing (for Phase 5)

- First-class MockBroker + expanded BrokerClient (account/clock/replace/close)
- Persisted Order Intents + manual approval workflow
- Formal order state machine with transition guards
- Dedicated reconciliation service with drift classification
- Live endpoint hard-block independent of dual-gate edge cases

## Incorrect or Risky

- Broker selection is explicit: `mock`, or `ibkr` when the connection flag is on. A missing provider does not silently become a live broker
- Dual orchestrators: legacy `WorkflowService` can submit when `enable_broker_orders=true`; daily SM still never submits (by design) — confusing for operators
- `enable_automated_execution` logged but not distinctly enforced vs manual approval

## Execution Integration Risks

- Wiring daily SM to orders without approval gates could bypass Phase 4/5 safety defaults
- Position sync delete+reinsert can drop local stop metadata

## Security Concerns

- Broker credentials must stay out of Agent Context (they are not passed — keep it that way)
- Live URL must be refused when `ENABLE_LIVE_TRADING=false` regardless of token mistakes

## Compatibility Concerns

- Existing `OrderManager` / `ExecutionValidator` / `ValidatedOrderIntent` must be bridged to new Intent/Approval layers without breaking paper tests

## Tests Executed

```bash
pytest tests/ -q
# 135 passed
```

## Corrections Applied

- Documented dual-path risk; Phase 5 makes broker provider explicit (`mock` default) and requires manual approval by default before any submit
- Phase 5 follow-up: `MockBroker` + factory, live URL hard-block, ExecutionService intent/approval path, OrderManager gated on flags
