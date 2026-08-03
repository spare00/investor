# Prompt Versioning

## Layout

```
prompts/
  shared/common_rules.md
  shared/output_contract.md
  {agent}/system_v1.md
```

Each agent file declares `Prompt-Version: x.y.z`.

## Runtime recording

`BaseAgent` loads the agent file + shared docs, computes SHA-256 over the concatenated text, and writes:

- `trace.prompt_version`
- `trace.prompt_sha256`
- `trace.schema_version`
- `trace.model_name` / `model_parameters`
- `trace.token_usage` / `latency_ms` when available

## Why this matters

- Changing a prompt without bumping the version still changes the hash → auditors can detect drift.
- Comparing historical CIO decisions requires knowing which prompt hash produced upstream reports.
- Replays should pin both model id and prompt hash; schema_version tracks Pydantic contract evolution.

## Change process

1. Edit `system_vN.md` (or add `system_vN+1.md` and point `prompt_file`).
2. Bump `Prompt-Version`.
3. Run prompt tests (`tests/unit/test_prompts_phase2.py`).
4. Note the change in release / phase report.

Legacy flat files `prompts/*_v0.1.0.txt` remain as fallback only if `system_v1.md` is missing.
