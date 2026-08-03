# State Machine

States: `DailyWorkflowState` in `app/workflow/states.py`.

## Per-state summary

| State | Enter | Work | Exit | Broker | Manual |
|-------|-------|------|------|--------|--------|
| NON_TRADING_DAY | Weekend/holiday prepare | Record + plan skip | → COMPLETED | No | Yes |
| PREMARKET_PREPARATION | Trading-day prepare | Session + job plan | → ANALYSIS | No | Yes |
| PREMARKET_ANALYSIS | After prep / reanalysis | 6-agent analysis | → REVALIDATION | No | Yes |
| PREOPEN_REVALIDATION | After analysis | Validity checks | → MARKET_OPEN / reanalysis / FAILED | No | Yes |
| MARKET_OPEN | Valid revalidation | Hand-off | → INTRADAY | No | Yes |
| INTRADAY | Market open | Reeval triggers | → CLOSING | No | Yes |
| CLOSING_WINDOW | Near close | ClosingPolicy only | → MARKET_CLOSED | No | Yes |
| MARKET_CLOSED | After close | — | → POSTMARKET | No | Yes |
| POSTMARKET_REVIEW | After closed | Minimal review | → COMPLETED | No | Yes |
| COMPLETED | Terminal | — | — | No | No |
| PAUSED | Ops pause | Block actions | Resume prior | No | Yes |
| EMERGENCY_STOP | Ops stop | Block all | → PAUSED (explicit clear) | No | Clear only |
| FAILED | Fail-closed | Persist reason | → PAUSED | No | Yes |

Illegal transitions raise `ValueError` via `assert_transition_allowed`.
`BROKER_ORDERS_ALLOWED` is `False` for every state in Phase 3.
