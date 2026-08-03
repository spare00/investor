# Security Audit — Phase 7

Date: 2026-08-04  
Scope: Performance/ops layer, dashboard, backup, readiness gates

## Summary

Phase 7 adds observability without widening the execution surface. **Live trading remains blocked.**

## Findings

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| SEC-701 | **Critical** | LIVE trading must stay disabled | Mitigated — `GateEvaluator` permanent `LIVE_NOT_ALLOWED`; dual-gate unchanged |
| SEC-702 | **High** | Dashboard exposes workflow POST buttons | Accepted — read-only for orders; no Live enable; DEV banner in non-prod |
| SEC-703 | **High** | Backup exports DB tables — no secrets in manifest | Mitigated — secret path filter |
| SEC-704 | **Medium** | No auth on `/performance/*`, `/operations/*` | Open — dev/paper only; not production-ready |
| SEC-705 | **Medium** | Alert webhook/email may leak context | Mitigated — operators must sanitize alert payloads |
| SEC-706 | **Low** | Fault injection gated off in production | Mitigated |
| SEC-707 | **Info** | Simulation runs store config hash, not secrets | OK |

## CLI check

```
python -m app.cli security
```

Prints path to this document and lightweight config checks.

## Not audited

- Third-party Alpaca API pen test
- LLM prompt injection regression suite (see Phase 4 docs)
