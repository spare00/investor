# Live Trading Readiness Checklist

> **Status: NOT READY** — Phase 7 explicitly blocks LIVE (`LIVE_NOT_ALLOWED`).

Operator promotion is manual only; `GateEvaluator.auto_promote` is always `false`.

## Gates (in order)

- [ ] DEVELOPMENT — local dev checks
- [ ] SIMULATION_READY — multi-day sims pass
- [ ] PAPER_OBSERVE_READY — observe-only paper stable
- [ ] PAPER_MANUAL_READY — manual approval path tested
- [ ] PAPER_AUTOMATED_CANDIDATE — automated paper candidate (not approved)
- [ ] PAPER_AUTOMATED_APPROVED — operator sign-off required
- [ ] ~~LIVE~~ — **NOT PERMITTED in Phase 7**

## Required before any future LIVE consideration (out of scope)

- [ ] Dual-gate LIVE env vars with non-default confirmation token
- [ ] AuthN/Z on all execution and ops endpoints
- [ ] 30+ days paper with reconciliation clean
- [ ] On-call runbook + paging tested
- [ ] Disaster recovery drill (backup restore)
- [ ] Legal/compliance sign-off
- [ ] Pen test / security audit closure

## Verify current block

```bash
python -m app.cli readiness evaluate
python -m app.cli security
curl -s localhost:8000/health | jq '.live_trading_allowed'
```

Expected: `live_trading_allowed: false`
