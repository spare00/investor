# Intraday Architecture (Phase 6)

## Identity

Intraday reanalysis is still the **6-agent firm**. The CIO produces an Intraday Decision;
when mode allows, intents are materialized the same way as premarket
(`materialize_cio_decision`). Humans do not replace the CIO.

## Modes

Ship default: `INTRADAY_OPERATION_MODE=OBSERVE_ONLY` — collect/monitor/analyze only
(scaffolding). Target paper firm mode: `PAPER_AUTOMATED`.

| Mode | Intents | Approve | Submit |
|------|---------|---------|--------|
| OBSERVE_ONLY | no | no | no |
| ANALYZE_ONLY | draft metadata | no | no |
| MANUAL_APPROVAL | yes | optional brake | if ENABLE_BROKER_ORDERS |
| PAPER_AUTOMATED | yes | auto when unlocked | paper only |
| PAUSED / EMERGENCY_STOP | blocked | blocked | blocked |

## Flow

```
Market/Broker Events → Event Bus (dedup/cooldown)
        ↓
Position Monitor + Dynamic Risk (deterministic)
        ↓
Intraday 6-Agent reanalysis → Intraday CIO Decision
        ↓
Order Intent (never direct Broker from Monitor/LLM)
        ↓
Pretrade → (optional manual brake) → ExecutionService / paper broker
```

Live trading remains hard-blocked. Automated paper submit stays off until
`ENABLE_BROKER_ORDERS` + `ENABLE_AUTOMATED_EXECUTION` are explicitly set.

Intraday evals share the scheduler 8-minute job cap. `LLM_RUNTIME=local|cloud` only
changes where chat runs (see `docs/agent_architecture.md`). Watch eval wall time vs
that cap as the watchlist grows (`committee_watch` on `/dashboard/summary`).
