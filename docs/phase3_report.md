# Phase 3 Report

Date: 2026-08-04  
Tests: `pytest tests/ -q` → **122 passed**

## Summary

Phase 3 adds US market calendar (DST/holidays/early close), a persistent daily
workflow state machine, DB leases, scheduler dispatch to workflow services only,
preopen revalidation, intraday reevaluation policy, closing policy interface,
postmarket review, recovery, and ops API/CLI — with broker orders disabled by default.

## Defaults

- `ENABLE_SCHEDULER=false`
- `ENABLE_AUTOMATED_EXECUTION=false`
- `ENABLE_BROKER_ORDERS=false`

## Docs

- `docs/phase2_audit.md`
- `docs/market_calendar.md`
- `docs/daily_workflow.md`
- `docs/state_machine.md`
- `docs/scheduler_and_leases.md`
- `docs/recovery.md`
- `docs/phase3_report.md`
