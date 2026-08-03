# Intraday Agents

Reuses Phase 2 `AgentPipeline` with rate-limited evaluation. Results stored as `intraday_decisions` (distinct from premarket CIO). Existing positions prioritized over new entries. Failure does not cancel protection orders. Default mode does not submit broker orders.
