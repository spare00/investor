# Prompt Injection Defense

External news/filings are untrusted:

- HTML/script/style stripped (`sanitize_external_text`)
- Length capped
- Null bytes rejected
- Explicit `<untrusted_data>` wrapper with instruction to ignore embedded commands
- System prompts remain separate from context payloads
