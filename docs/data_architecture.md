# Data Architecture (Phase 4)

```
External Providers → Provider Adapters → Canonical Models
  → Validation / Dedup / Freshness / Quality / Conflicts
  → Market Events → Context Builders → 6-Agent Workflow
```

- Adapters never call Broker or LLM.
- Default: `ENABLE_EXTERNAL_DATA=false` → fixture providers only.
- Real adapters: Alpaca market quotes (needs keys + flags), SEC EDGAR filings (needs `ENABLE_SEC_COLLECTION`).
- Legacy `CollectionBundle` is still produced for `AgentPipeline` compatibility.

See also: `provider_adapters.md`, `canonical_models.md`, `data_quality.md`.
