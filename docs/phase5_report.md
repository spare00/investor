# Phase 5 Report — Broker & Paper Trading Execution Layer

Date: 2026-08-04

## 1. Phase 4 Audit Summary

See `docs/phase4_audit.md`. Core data layer verified; fixture mode works without credentials. Dual orchestrator risk documented.

## 2. Corrections Applied Before Phase 5

Explicit `BROKER_PROVIDER=mock` default; OrderManager gated; live URL refused on Alpaca adapter init.

## 3. Broker Architecture Decisions

Factory + Mock/Alpaca adapters; shared `BrokerError`; REST/httpx for Alpaca; ExecutionService owns intent path.

## 4–21. Implementation map

| Area | Location |
|------|----------|
| Canonical models / SM | `app/brokers/models.py` |
| MockBroker | `app/brokers/mock.py` |
| Alpaca Paper | `app/brokers/alpaca.py` |
| Factory / live block | `app/brokers/factory.py` |
| Sizing | `app/execution/sizing.py` |
| Pretrade | `app/execution/pretrade.py` |
| Execution policy | `app/execution/policy.py` |
| Intents / approve / submit | `app/execution/service.py` |
| Reconciliation | `app/execution/reconciliation.py` |
| API | `app/api/broker.py`, `app/api/execution.py` |
| Migration | `migrations/versions/0004_phase5_broker_execution.py` |

## 22. Daily Workflow Integration

Intents built when CIO produces actions; auto-submit only if `ENABLE_BROKER_ORDERS` + `ENABLE_AUTOMATED_EXECUTION` + not manual approval. Defaults never auto-submit.

## 23. API and CLI

`/broker/*`, `/execution/*`, `/operations/emergency-stop*`; CLI `broker` and `execution` subcommands.

## 24. Database

Tables: `order_intents`, `order_approvals`, `pretrade_risk_checks`, `broker_reconciliation_runs`. Unique: `client_order_id`, orders `idempotency_key`.

## 25–27. Files / tests

Added broker factory/mock/models/errors, execution service/pretrade/sizing/policy/reconciliation, API routers, Phase 5 unit tests, and architecture docs under `docs/`.

## 28. Test Commands and Results

```bash
pytest tests/ -q
# 148 passed, 1 skipped
```

Skipped: `test_alpaca_paper_smoke_opt_in` (requires `RUN_ALPACA_PAPER_SMOKE_TESTS=true`).

## 29. Alpaca Paper Smoke Test Status

**Not executed.** Opt-in flag was not set; no paper credentials were used. No Alpaca paper orders were submitted.

## 30. Broker Order Safety Verification

Defaults: mock provider, connection/orders/automation off, manual approval on, live trading false. Live URL construction raises. No free-form manual order API.

## 31. Known Limitations

Protection orders not auto-submitted; no streaming; pretrade sector checks lean on sizing caps; material drift is recorded for operator review.

## 32. Deferred Items

Full position lifecycle monitors, bracket/OCO live wiring, wash-sale hooks, secret manager adapter.

## 33. Recommended Phase 6 Scope

Phase 5 audit, streaming/near-RT events, Position Monitor, intraday HOLD/ADD/REDUCE, dynamic risk revalidation, exit/closing policy execution, overnight review, post-trade review.
