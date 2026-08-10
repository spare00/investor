# Postmarket Settlement

Syncs account/positions/orders via reconciliation + position manager, computes FIFO P&L (`POSITION_LOT_METHOD=FIFO`), records overnight holdings. Broker is source of truth for balances; decision history is preserved.

Settlement is **venue-scoped**: `settle(session_date=…, venue=US|AU)` filters open lifecycles by book, tags `payload.venue`, and uses distinct MARKET_CLOSED / TradePnL keys so US and AU postmarket on the same calendar date do not overwrite each other.

## Unattended path

Scheduler `postmarket_review` → `DailyWorkflowService.run_postmarket` calls `SettlementService.settle`, creates light `PostTradeReview` rows for closed/pending-close lifecycles, and runs `PerformanceService.recalculate` + `evaluate_decisions_batch(persist=True)`. Failures are recorded under `review.*_error` and do not block `COMPLETED`.
