# Operations Runbook (Phase 7)

Commands match `python -m app.cli` and HTTP APIs.

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
