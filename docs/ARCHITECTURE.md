# Architecture notes (Phase 1)

## Intent

Build a bottom-up multi-agent US equity system that **controls loss** and compounds
capital under paper trading. Execution is deliberately delayed until risk and
workflow contracts are solid.

## Layer responsibilities

| Layer | Owns | Must not own |
|-------|------|--------------|
| Collectors | Fetch, dedupe, freshness scores | Trade decisions |
| Storage | Persistence, audit fields | Business veto rules |
| Agents | Structured analysis JSON | Direct broker calls |
| Risk Engine | Hard limits, sizing, vetoes | Narrative thesis writing |
| Decision/Workflow | Ordering, parallelism, retries | Raw HTTP to brokers |
| Execution | Idempotent order submit/cancel | Changing risk policy |
| Monitoring | Metrics, reviews, alerts | Silent exception swallowing |

## Supplements vs original brief

1. Dual-gate live trading enablement.
2. Dedicated `app/risk` for deterministic math/tests.
3. Explicit fail-closed security helper (`is_live_trading_allowed`).
4. News/market provider interfaces with stub implementations.
5. Exchange calendar dependency for holidays/early closes.
6. Dual display timezones (US Eastern + Brisbane) with UTC storage.
7. Idempotency key in order intent schemas before execution exists.
8. Separate halt-day threshold (5 losses) from cooldown threshold (3 losses).

## Trust boundaries

```
LLM Agents ──JSON schemas──► Decision Validator ──► Risk Engine ──► Execution
                                      ▲
                                      │ Hard Veto (non-overridable)
```

CIO output is advisory until Risk Engine + Execution Validator approve.

## Phase 1 out of scope

- Real Alpaca order placement
- Full SQLAlchemy table migrations for all domain entities
- Agent LLM calls
- Scheduler jobs
- Dashboard UI
