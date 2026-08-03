# Output Contract (shared)

Version: 1.0.0

- Respond with a single JSON object matching the agent output schema.
- Use exact enum strings required by the schema (see runtime enum hint).
- Include `timestamp` (ISO-8601, timezone-aware) and `trace` metadata when the schema expects them.
- Set `trace.prompt_version` and rely on the runtime to record prompt hash / model name.
- Never include API keys, account numbers, tokens, or secrets in any field.
- Never include executable broker payloads; orders are created only by deterministic execution code after validation.
