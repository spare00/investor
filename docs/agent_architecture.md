# Agent Architecture

## Project identity

`investor` is an **agentic AI virtual investment firm** for US equities. It is independent of `stocktrader` (a separate profile-based automation tool). Investor talks to brokers (e.g. Alpaca) through its own adapter layer — never through stocktrader.

## Six roles

| Agent | Role | Trades? |
|-------|------|---------|
| Market Intelligence | Fact set from news/events | No |
| Macro Strategist | Regime classification | No |
| Quant Strategist | Technical/structure interpretation of **pre-computed** metrics | No |
| Risk Manager | Soft LLM review + **deterministic Hard Veto engine** | Approve/veto only |
| Devil’s Advocate | Challenge theses | No |
| CIO | Final portfolio/symbol actions (decision object) | Decision JSON only |

## Bottom-up data flow

```
Collection / fixtures
        ↓
Market Intelligence
        ↓
Macro Strategist  ∥  Quant Strategist
        ↓
Risk Manager (engine + optional LLM soft warnings)
        ↓
Devil’s Advocate
        ↓
CIO Decision
        ↓
(Later phases) Execution Validator → Broker adapter
```

Higher agents receive structured Pydantic reports plus selected raw references — not free-text paste dumps as the sole contract.

## LLM vs deterministic code

| Concern | Owner |
|---------|--------|
| Indicators (RSI, ATR, SMA, sizes, exposures) | Deterministic Python |
| Hard risk vetoes, sizing caps | `app/risk` engine |
| Narrative synthesis, regime judgment, challenges | LLM agents |
| Broker HTTP | `app/brokers` only — never LLM |

## Hard Veto

Deterministic engine vetoes are authoritative. CIO schema validation rejects risk-increasing actions when `risk_approval` is false. Execution validator re-checks before any submit.

## Prompts

Runtime loads `prompts/{agent}/system_v1.md` plus `prompts/shared/common_rules.md` and `output_contract.md`. Prompt version and SHA-256 are recorded on `trace`.

## Ahead of Phase 2 (kept)

Paper Alpaca execution, dashboard, and session workflows exist in-tree for later roadmap phases. Phase 2 analysis entrypoints (`POST /workflow/analysis/run`, `python -m app.cli run-analysis`) do **not** submit orders.
