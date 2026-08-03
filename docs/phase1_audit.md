# Phase 1 Audit

Date: 2026-08-04  
Repo: `investor/` (not `stocktrader`)

## Implemented

- Project scaffolding: FastAPI, Pydantic settings, structured logging, SQLAlchemy async + Alembic
- Config dual-gate for live trading; paper default
- Six agent classes with Pydantic I/O under `app/schemas/` and `app/agents/`
- Deterministic Risk Engine (`app/risk/`) with unit tests
- Bottom-up pipeline: MI → Macro∥Quant → Risk → Devil → CIO (`app/agents/pipeline.py`)
- LLM adapter: OpenAI-compatible client + stub/fake client (`app/services/llm.py`)
- Broker interface + Alpaca paper path (ahead of original Phase 1/2 scope; kept)
- No `stocktrader` dependency
- Secrets: `.env` gitignored; `.env.example` placeholders only
- Unit tests: 78 passing at audit time (`pytest tests/unit`)

## Partially Implemented

- Prompts: flat `prompts/*_v0.1.0.txt` role blurbs — not the Phase 2 folder layout or full operating procedures
- Common rules hardcoded in `app/agents/base.py` instead of `prompts/shared/`
- Prompt version recorded; **prompt SHA-256 hash not recorded**
- Analysis APIs exist (`/workflow/premarket/analyze`) but not the Phase 2 `POST /workflow/analysis/run` contract
- Agent run ORM exists; metadata (prompt hash, token usage, schema_version) incomplete
- README claimed “Phase 1–7 complete” under a different phase numbering than this roadmap

## Missing (relative to Phase 2 brief)

- `prompts/shared/common_rules.md`, `output_contract.md`
- `prompts/{agent}/system_v1.md` with Identity…Quality Checklist sections
- FakeLLMProvider naming (functionally StubLLMClient)
- Prompt/hash/versioning docs
- Dedicated Phase 2 prompt/framework tests
- CLI `run-analysis --fixture`
- Several Phase 2 schema field names (optional enrichment needed)

## Incorrect or Risky

- Docs vs code: old README phase table overstated “complete” for items that were MVP vs this stricter Phase 2 prompt contract
- Premarket workflow can submit paper orders — fine for later phases, but Phase 2 analysis path must remain broker-free
- Scheduler jobs are no-op ticks (documented; not a Phase 2 blocker)
- Market data in development often falls back to stub quotes — OK for offline tests; must not be treated as live prices for limit exits (already mitigated with market exits)

## Tests Executed

```bash
pytest tests/unit -q
# 78 passed (pre Phase 2 changes)
```

## Recommended Corrections (applied in Phase 2)

1. Introduce shared + per-agent `system_v1.md` prompts; load + hash in BaseAgent
2. Alias FakeLLMProvider; keep OpenAICompatibleClient adapter boundary
3. Add `POST /workflow/analysis/run` (no broker submit) + fixture CLI
4. Extend schemas with optional Phase 2 fields / `INSUFFICIENT_DATA` without breaking pipeline
5. Cap LLM validation repair to one retry then safe NO_TRADE / fail-closed
6. Add Phase 2 docs and prompt tests
7. Keep Alpaca/dashboard/scheduler as **ahead-of-roadmap** code; do not rip out
