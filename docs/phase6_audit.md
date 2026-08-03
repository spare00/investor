# Phase 6 Audit (pre Phase 7)

Date: 2026-08-04  
Command: `pytest tests/ -q` → **161 passed, 2 skipped**

## Implemented Correctly

- Mode gating (OBSERVE_ONLY default); exits via Intent path, not direct broker
- Event dedup + revision; stop widening blocked; take-profit partial indices
- FIFO PnL helper; recovery order (emergency → recon → poll)
- Live trading / automated execution remain off by default
- Mock intraday E2E simulation test passes

## Partially Implemented

- Position snapshots lack `risk_amount` / real data_quality (Phase 7 fills MAE/MFE & metrics)
- Post-trade review shell without quality scores (Phase 7 evaluates)
- Broker polling only on recovery path (documented; Phase 7 ops KPIs track unknown orders)

## Missing

- Scheduler not wired to `IntradayService` monitor/closing/settlement (manual API/CLI only)
- Event bus is persistence + flags without automatic consumer dispatch
- `protection_submitted` / `reconciliation_required` rarely set true in production paths

## Incorrect or Risky

- Settlement was non-idempotent / unbounded executions (corrected before Phase 7)
- Risk events mislabeled WARNING when status is EXIT (corrected priority/heuristic)
- `monitor_all` passed zero daily PnL/drawdown (corrected to use portfolio snapshot when available)

## Performance Measurement Risks

- Dual stores (`DailyPerformance` vs `TradePnL`) — Phase 7 valuation/metrics are canonical for reporting
- Duplicate settlement could corrupt PnL history — fixed with session_date upsert

## Operational Risks

- Unattended intraday loop requires scheduler bridge (documented limitation; Phase 7 readiness gates require manual/scheduled ops)

## Security Concerns

- No new credential leaks found; account redaction on snapshots retained

## Data Integrity Concerns

- Settlement idempotency + date scope applied before Phase 7 metrics

## Tests Executed

```bash
pytest tests/ -q
# 161 passed, 2 skipped
```

## Corrections Applied

- Settlement upsert by `session_date`; executions scoped to session day when timestamps exist
- Risk event type/priority for EXIT/EMERGENCY
- `monitor_all` loads latest portfolio daily_pnl/drawdown/equity when present
- `enable_intraday_monitoring` gate on `monitor_all`
