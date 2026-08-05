# Overnight Risk

Per-position overnight review valid for the session date only. Results: OVERNIGHT_APPROVED, WITH_REDUCTION, CLOSE_BEFORE_MARKET_CLOSE, MANUAL_REVIEW_REQUIRED, NO_DATA.

## Unattended path

`DailyWorkflowService.start_closing` and `run_postmarket` call `ClosingService.overnight_review` after force-close / before settlement.

- `next_session_holiday` is set when `MarketCalendarService.next_session_has_holiday_gap` finds a weekday holiday between this session and the next.
- Flagged statuses emit `trading.overnight_review` (WARNING / CRITICAL for manual review).
- Latest rows surface on Overview (`overnight_reviews` in `/dashboard/summary`).
