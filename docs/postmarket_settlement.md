# Postmarket Settlement

Syncs account/positions/orders via reconciliation + position manager, computes FIFO P&L (`POSITION_LOT_METHOD=FIFO`), records overnight holdings. Broker is source of truth for balances; decision history is preserved.

## Unattended path

Scheduler `postmarket_review` → `DailyWorkflowService.run_postmarket` calls `SettlementService.settle`, creates light `PostTradeReview` rows for closed/pending-close lifecycles, and runs `PerformanceService.recalculate` + `evaluate_decisions_batch(persist=True)`. Failures are recorded under `review.*_error` and do not block `COMPLETED`.
