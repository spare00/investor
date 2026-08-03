# Intraday Architecture (Phase 6)

## Modes

Default: `INTRADAY_OPERATION_MODE=OBSERVE_ONLY` — collect/monitor/analyze only; no broker submits.

| Mode | Intents | Approve | Submit |
|------|---------|---------|--------|
| OBSERVE_ONLY | draft metadata only | no | no |
| ANALYZE_ONLY | draft | no | no |
| MANUAL_APPROVAL | yes | yes | if ENABLE_BROKER_ORDERS |
| PAPER_AUTOMATED | yes | auto if configured | paper only |
| PAUSED / EMERGENCY_STOP | blocked | blocked | blocked |

## Flow

```
Market/Broker Events → Event Bus (dedup/cooldown)
        ↓
Position Monitor + Dynamic Risk (deterministic)
        ↓
Optional Intraday 6-Agent reanalysis → Intraday CIO Decision
        ↓
Order Intent (never direct Broker from Monitor/LLM)
        ↓
Pretrade → Approval → ExecutionService
```

Streaming is optional (`BROKER_STREAMING_ENABLED=false`); polling fallback is default.

Live trading remains hard-blocked. Automated execution stays off by default.
