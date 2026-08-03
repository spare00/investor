# Simulation Framework

## Runner

`MultiDaySimulationRunner` — deterministic multi-day scenarios using `MockBroker` + `FakeLLM`. No real broker or external LLM calls.

## Scenarios

`bull-market`, `bear-market`, `sideways`, `volatility-shock`, `provider-outage`, `broker-outage`, `emergency-stop`, `drawdown`, `early-close`

## API / CLI

| Command | Description |
|---------|-------------|
| `POST /simulations/run` | Start run |
| `GET /simulations` | List persisted runs |
| `GET /simulations/{id}` | Detail |
| `POST /simulations/{id}/cancel` | Mark cancelled |
| `python -m app.cli simulation run --scenario bull-market --days 5` | CLI |
| `python -m app.cli simulation report --id <uuid>` | Report |

## Persistence

`simulation_runs` table via `SimulationRunRecord`.

## Limitations

- Synthetic price paths, not historical replay
- Agent pipeline not fully replayed per day (LLM stub only)
- `ENABLE_LONG_RUNNING_SIMULATION=false` caps days at 30
- Cancel only updates DB status; no in-flight thread to interrupt
