# Daily Workflow

Persistent per-session orchestration for one US equity trading day.

**Trading actor:** CIO (bottom-up) after the 6-agent pipeline. Daily workflow
materializes Order Intents from the CIO decision. Paper broker submit occurs when
`ENABLE_BROKER_ORDERS` + `ENABLE_AUTOMATED_EXECUTION` are unlocked and Live is off.
Manual approval is an optional ops brake only.

## Flow (US Eastern)

Times below are **relative to Calendar Service open/close**, not fixed wall clocks.

```
NON_TRADING_DAY → COMPLETED
   or
PREMARKET_PREPARATION
  → PREMARKET_ANALYSIS          (6-Agent → CIO → Order Intents → optional paper submit)
  → PREOPEN_REVALIDATION
  → MARKET_OPEN → INTRADAY      (monitor ticks + interval/risk CIO reanalysis)
  → CLOSING_WINDOW              (ClosingService force-flatten intents)
  → MARKET_CLOSED
  → POSTMARKET_REVIEW           (settlement + posttrade + performance eval)
  → COMPLETED
```

## Broker boundary

Ship defaults keep orders off (`ENABLE_BROKER_ORDERS=false`) so undeployed
environments fail closed. That is scaffolding — **not** “humans trade by default.”

When paper automation is armed, `DailyWorkflowService.run_analysis` calls
`materialize_cio_decision` (`app/execution/firm_execution.py`).
Live trading remains hard-blocked.
