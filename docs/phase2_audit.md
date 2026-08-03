# Phase 2 Audit (pre Phase 3)

Date: 2026-08-04  
Command: `pytest tests/unit -q` → **93 passed**

## Implemented Correctly

- Six `prompts/{agent}/system_v1.md` files loaded via `app/agents/prompts.py` + shared rules/contract
- Prompt version + SHA-256 written onto `trace` in `BaseAgent`
- `FakeLLMProvider` alias; CLI `--fake-llm` and analysis path use stubs/fallbacks without requiring live LLM
- `POST /workflow/analysis/run` and `python -m app.cli run-analysis` set `broker_orders_submitted: false`
- Hard Veto honored in CIO schema + risk engine
- No stocktrader dependency

## Partially Implemented

- `schema_version` / `token_usage` / `latency_ms` on TraceMetadata — recorded when LLM path succeeds; fallbacks may omit full token usage
- Analysis idempotency still **in-process memory** (`_ANALYSIS_IDEMPOTENCY`) — Phase 3 replaces with DB leases
- Scheduler jobs are no-op ticks; not connected to analysis workflow
- Phase 2 schema field rename map incomplete (optional fields only)

## Missing (for Phase 3)

- Market calendar service / session status API
- Persistent daily workflow state machine
- DB leases / recovery
- Preopen revalidation & closing policy interfaces
- Scheduler → workflow service wiring with `ENABLE_BROKER_ORDERS=false`

## Incorrect or Risky

- `scheduler_enabled` defaulted True while Phase 3 requires scheduler off by default for safe ops — correct in Phase 3 config
- Premarket `/workflow/premarket/run` can still submit paper orders if called manually — keep boundary: daily state machine must not call it when broker flags are false

## Compatibility Concerns

- Dual phase numbering in README (legacy 1–7 vs roadmap 2–7) — documented, not blocking
- Existing Alpaca/dashboard code stays; must not be driven by Phase 3 scheduler

## Tests Executed

```bash
pytest tests/unit -q
# 93 passed
```

## Corrections Applied

- None blocking before Phase 3 start; Phase 3 introduces calendar, leases, and `enable_scheduler=false` / `enable_broker_orders=false` defaults
