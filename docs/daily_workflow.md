# Daily Workflow

Persistent per-session orchestration for one US equity trading day.

## Flow (US Eastern)

Times below are **relative to Calendar Service open/close**, not fixed wall clocks.
Brisbane display times shift when US DST changes (Brisbane does not observe DST).

```
NON_TRADING_DAY → COMPLETED
   or
PREMARKET_PREPARATION
  → PREMARKET_ANALYSIS          (6-agent chain, no broker)
  → PREOPEN_REVALIDATION
  → MARKET_OPEN → INTRADAY
  → CLOSING_WINDOW              (policy interface only)
  → MARKET_CLOSED
  → POSTMARKET_REVIEW
  → COMPLETED
```

Configurable offsets (defaults):

| Setting | Default |
|---------|---------|
| Premarket prepare before open | 180 min |
| Premarket analysis before open | 120 min |
| Preopen revalidation before open | 10 min |
| Intraday reevaluation interval | 20 min |
| Closing window before close | 30 min |
| Postmarket after close | 30 min |

Early-close days use the calendar’s actual `regular_close`, not a hard-coded 16:00.

## Brisbane note

Example: regular open 09:30 America/New_York converts to Brisbane via `astimezone(Australia/Brisbane)`.
In US winter that is typically **00:30 next calendar day** BNE; in US summer typically **23:30 same calendar day** BNE.
Always derive from Calendar Service / `zoneinfo`, never from a fixed offset table.

## Broker boundary

`ENABLE_BROKER_ORDERS=false` and `ENABLE_AUTOMATED_EXECUTION=false` by default.
`DailyWorkflowService` never submits orders; closing plans are advisory.
Existing `/workflow/premarket/run` may still paper-trade if invoked manually — daily SM does not call it.
