# Phase 2 Report

## Scope delivered

- Phase 1 audit (`docs/phase1_audit.md`)
- Full prompt tree under `prompts/shared/` and `prompts/{agent}/system_v1.md`
- Prompt loader with version + SHA-256 (`app/agents/prompts.py`)
- BaseAgent loads shared rules; records hash/latency/token metadata on trace
- FakeLLMProvider alias for StubLLMClient
- One validation repair then fail (fallback may still produce safe output)
- `POST /workflow/analysis/run` (no broker) + Idempotency-Key
- CLI: `python -m app.cli run-analysis --fixture … --fake-llm`
- Docs: agent architecture, prompt versioning, this report
- Prompt/framework unit tests

## Explicitly not redone (ahead / deferred)

- Live news/market providers (Phase 4)
- Alpaca paper execution already present — left intact; analysis path does not call it (Phase 5+)
- Full scheduler state machine / DST calendar (Phase 3)
- Dashboard polish / performance analytics (Phase 6)

## Schema notes

- Added `MarketRegime.INSUFFICIENT_DATA`
- Extended `TraceMetadata` with prompt hash, schema_version, token_usage, latency_ms
- Devil `recommendation` / `challenge_severity` optional fields
- Full Phase 2 field rename map not force-migrated where it would break production pipeline; optional enrichment preferred

## Recommended Phase 3 focus

US market calendar, DST, holiday/early close, real scheduler → workflow runs, lease/recovery, pre-open revalidation, intraday cadence (15–30m), force-close policy.
