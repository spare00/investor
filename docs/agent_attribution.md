# Agent Attribution

## Scope

Measure whether agent directional views and warnings matched realized outcomes.

## Tables

- Legacy: `agent_outcome_evaluations` (Phase 6 shell)
- Phase 7: `agent_evaluations` (extended scoring fields)

## API

- `GET /performance/agents`
- `POST /performance/evaluate-agents`

## Metrics

Direction accuracy, abstention rate, warning usefulness (when payload populated).

## Not supported

- Per-claim NLP verification against news corpus
- Automatic prompt/version rollback based on scores
- Cross-agent Shapley-style attribution
