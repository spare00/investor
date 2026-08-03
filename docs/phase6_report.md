# Phase 6 Report — Intraday Operations & Position Management

Date: 2026-08-04

## Summary

Phase 6 adds event bus, position lifecycle/monitor, dynamic risk, stops/TP, closing/overnight, settlement/FIFO PnL, post-trade review hooks, intraday agent orchestration, recovery, API/CLI. Defaults remain OBSERVE_ONLY with no automated broker orders and live trading blocked.

## Tests

```bash
pytest tests/ -q
```

Alpaca intraday smoke: **not run** (`RUN_ALPACA_PAPER_INTRADAY_SMOKE_TESTS` unset).

## Known limitations

- Broker streaming not enabled (polling fallback only)
- Intraday agent evaluate uses collection stubs / fake LLM in tests
- Hard stops create intents but do not auto-submit by default
- Full MAE/MFE and Agent scoring deferred to Phase 7
