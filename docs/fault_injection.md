# Fault Injection

## Purpose

Test fail-closed behavior in development/test only.

## Module

`app/ops/fault_injection.py` — `FaultInjectionFramework`, `FaultKind` (PROVIDER_OUTAGE, BROKER_OUTAGE, …).

## Gating

- Requires `ENABLE_FAULT_INJECTION=true`
- **Blocked in production** — raises `FaultInjectionError`

## Not supported

- HTTP API for injection (CLI/internal only)
- Chaos during live trading (live blocked anyway)
- Automated chaos schedules

See `tests/unit/test_phase7_ops.py`.
