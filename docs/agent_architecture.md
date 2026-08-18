# Agent Architecture

## Project identity

`investor` is an **agentic AI virtual investment firm** for US (and optional AU) equities. It talks to brokers through `app/brokers` (IBKR paper today) — never through another trading app, and never from LLM code.

## Six roles

| Agent | Role | Trades? |
|-------|------|---------|
| Market Intelligence | Fact set from news/events | No |
| Macro Strategist | Regime classification | No |
| Quant Strategist | Technical/structure interpretation of **pre-computed** metrics | No |
| Risk Manager | Soft LLM review (cloud) + **deterministic Hard Veto engine** | Approve/veto only |
| Devil’s Advocate | Challenge theses | No |
| CIO | Final portfolio/symbol actions (decision object) | Decision JSON only |

Universe Manager is a seventh LLM slot used on a slow refresh cadence, not on every intraday eval.

## LLM runtime (`LLM_RUNTIME`)

The pipeline stays the same. Inference target is a switch, not a fork of the firm:

| `LLM_RUNTIME` | Chat backend | Spend cap | Typical operator use |
|---------------|--------------|-----------|----------------------|
| `cloud` (Settings default) | OpenAI-compatible URL (`LLM_BASE_URL` / `LLM_MODEL`, e.g. `gpt-4o-mini`) | Monthly AUD / daily token budget | Billable API, Macro∥Quant in parallel |
| `local` | On-box Ollama (or any loopback OpenAI-compatible server) | Off — Python skip + 8-minute job cap instead | Paper firm on this Mac |

`GET /health` reports `llm_runtime`, `llm_is_local`, `llm_model`, and per-agent `agent_roles`. When local, leftover `gpt-*` / `openai.com` values in `.env` are rewritten to the local model and loopback URL. Flip back with `LLM_RUNTIME=cloud` plus a real API key — agent code does not need a rewrite.

Local 14B must finish inside the scheduler **8-minute** `job_action_timeout` (`JOB_ACTION_TIMEOUT_SECONDS_LOCAL=480`, same cap as cloud). Compact QUESTION/DATA/ANSWER briefs keep request `num_ctx` at 4k–8k. Optional `LLM_LOCAL_FAST_MODEL` (e.g. `qwen2.5:7b`) can take the “fast” slot; empty means every chat agent uses the 14B decision model.

## Python vs LLM (local vs cloud)

Python always owns indicators, Hard Vetoes, and broker HTTP. Chat is only for judgments a human would still have to make. **Trading style is per book** (`app/universe/book_strategy.py`): 초단타 / 단타 / 단기 each have their own entry, exit, and size rules. 중기 is skipped for new risk.

| Agent | Python owns | Cloud LLM | Local LLM (`LLM_RUNTIME=local`) |
|-------|-------------|-----------|----------------------------------|
| Market Intelligence | News fetch, dedupe, symbol tagging | Themes / importance | Same chat, smaller ctx |
| Macro Strategist | Rates/CPI/curve snapshot | `market_regime` | Same chat |
| Quant Strategist | OHLCV, SMA/RSI/ATR, horizon stops | Tape narrative | **Skip chat** — Python fallback |
| Risk Manager | Hard Veto engine | Soft warnings | **Skip chat** — engine only |
| Devil’s Advocate | Compact upstream briefs | Prefer-no-trade + counterpoint | Same chat |
| CIO | Positions, allowlist, stop enrichment | Portfolio/symbol actions | Same chat |

`app/agents/roles.py` is the source of truth (`skip_llm_when_local`, `num_ctx`, `max_tokens`, `model_slot`). Outcome `python` / `python-rules` / `risk-engine` on the dashboard is a successful local skip, not a failed LLM call.

## Bottom-up data flow

```
Collection / fixtures
        ↓
Market Intelligence
        ↓
Macro Strategist  ∥  Quant Strategist     (cloud: parallel)
Macro → Quant                             (local: sequential, one GPU)
        ↓
Risk Manager (engine; cloud may add LLM soft warnings)
        ↓
Devil’s Advocate
        ↓
CIO Decision
        ↓
Execution Validator → Broker adapter
```

Higher agents receive structured Pydantic reports plus selected raw references — not free-text paste dumps as the sole contract.

## Hard Veto

Deterministic engine vetoes are authoritative. CIO schema validation rejects risk-increasing actions when `risk_approval` is false. Execution validator re-checks before any submit.

## Prompts

Runtime loads `prompts/{agent}/system_v1.md` plus `prompts/shared/common_rules.md` and `output_contract.md`. Prompt version and SHA-256 are recorded on `trace`. Local runs use shorter decision-first briefs so 14B stays inside the job cap.

## Growing the book

More managed symbols lengthen collection + briefs + (on cloud) Quant/Risk chat. Watch wall time vs the 8-minute cap — see [operations_runbook.md](operations_runbook.md#committee-wall-time-watch) and `committee_watch` on `GET /dashboard/summary`.

## Analysis vs orders

`POST /workflow/analysis/run` and `python -m app.cli run-analysis` do **not** submit orders. Paper submits stay behind the usual execution flags.
