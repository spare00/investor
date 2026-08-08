# Agent Attribution

## Scope

Measure whether agent directional views and warnings matched realized outcomes.

## Tables

- Legacy: `agent_outcome_evaluations` (Phase 6 shell)
- Phase 7: `agent_evaluations` (extended scoring fields)

## API

- `GET /performance/agents` — firm metrics plus `by_horizon` and `by_agent` slices
- `POST /performance/evaluate-agents`

## Metrics

Direction accuracy, abstention rate, Brier, calibration gap/ECE (when payload populated).

Horizon book comes from `payload.universe_horizon` (stamped from lifecycle `exit_policy.horizon` at post-trade). PnL is used as a directional return proxy when `actual_return` is missing.

## Not supported

- Per-claim NLP verification against news corpus
- Automatic prompt/version rollback based on scores
- Cross-agent Shapley-style attribution
- Auto strategy mutation from attribution scores