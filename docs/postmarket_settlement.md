# Postmarket Settlement

Syncs account/positions/orders via reconciliation + position manager, computes FIFO P&L (`POSITION_LOT_METHOD=FIFO`), records overnight holdings. Broker is source of truth for balances; decision history is preserved.

Settlement is **venue-scoped**: `settle(session_date=…, venue=US|AU)` filters open lifecycles by book, tags `payload.venue`, and uses distinct MARKET_CLOSED / TradePnL keys so US and AU postmarket on the same calendar date do not overwrite each other.

## Unattended path

Scheduler `postmarket_review` → `DailyWorkflowService.run_postmarket` calls `SettlementService.settle`, creates light `PostTradeReview` rows for **today's** closed/pending-close lifecycles, then marks the session `COMPLETED` and enqueues `postmarket_eval_0`. Overnight review is reused from the closing window when already present. Each review step has its own timeout (settlement 60s, overnight/posttrade 20s) so a hung IBKR call cannot pin the 8-minute scheduler cap or leave the session in `POSTMARKET_REVIEW`. Failures are recorded under `review.*_error` and do not block `COMPLETED`.

Decision scoring is split across follow-up `postmarket_eval_*` jobs (12 decisions per 60s slice). Each chunk skips CIO decisions that already have an evaluation row, and the first job of the session re-does leftover `PENDING` rows. There is no practical job-count cap — leftover 90-day eval work continues on later ticks instead of being dropped.
