# Operations Runbook (Phase 7)

Commands match `python -m app.cli` and HTTP APIs.

## Paper firm arming (unattended)

Fail-closed defaults keep orders off. To run the 6-agent paper loop:

1. `.env`: `TRADING_MODE=paper`, `BROKER_ENVIRONMENT=paper`, `INTRADAY_OPERATION_MODE=PAPER_AUTOMATED`
2. Unlock paper submits: `ENABLE_BROKER_ORDERS=true`, `ENABLE_AUTOMATED_EXECUTION=true`, `REQUIRE_MANUAL_ORDER_APPROVAL=false`
3. Scheduler: `ENABLE_SCHEDULER=true` (plans `intraday_eval_*`, `closing_window`, optional `universe_refresh`)
4. Monitoring: `ENABLE_INTRADAY_MONITORING=true` (stop/TP ticks escalate to CIO on each eval)
5. Force flatten near close (optional): `AUTO_EXECUTE_FORCE_CLOSE=true` — otherwise closing creates intents only
6. Universe: `UNIVERSE_MODE=dynamic`, `UNIVERSE_MANAGER_ENABLED=true`

Verify:

```bash
python -m app.cli daily-workflow prepare
python -m app.cli scheduler list
python -m app.cli universe show
python -m app.cli closing run   # or wait for closing_window job
curl -s localhost:8000/dashboard/summary | jq '{workflow:.market_status.workflow,force_close,session_jobs:(.session_jobs|length)}'
```

Overview shows reeval budget (`reeval used/max`), planned interval, session job plan, paused hygiene names, and force-close arming.

## Daily observe (paper)

```bash
python -m app.cli market-status
python -m app.cli daily-workflow status
python -m app.cli intraday status
curl -s localhost:8000/dashboard/summary | jq .
```

## Performance review

```bash
python -m app.cli performance portfolio
python -m app.cli performance risk
python -m app.cli performance drawdowns
python -m app.cli performance trades
python -m app.cli performance agents
python -m app.cli performance recalculate
curl -s -X POST localhost:8000/performance/recalculate | jq .
```

## Providers & ops

```bash
python -m app.cli providers health
python -m app.cli providers reliability
python -m app.cli operations metrics
python -m app.cli alerts list
python -m app.cli alerts acknowledge --id <uuid>
```

## Readiness (LIVE blocked)

```bash
python -m app.cli readiness evaluate
curl -s localhost:8000/operations/readiness | jq .
curl -s -X POST localhost:8000/readiness/evaluate | jq .
```

## Simulation

```bash
python -m app.cli simulation run --scenario bull-market --days 5
python -m app.cli simulation report --id <uuid>
```

## Backup

```bash
python -m app.cli backup create
python -m app.cli backup verify --path backups/<id>.zip
```

## Security

```bash
python -m app.cli security
```

## Emergency

```bash
curl -X POST localhost:8000/trading/emergency-stop
python -m app.cli recovery run
```

Live trading enablement is **not** in this runbook — see `live_trading_readiness_checklist.md` (NOT READY).
