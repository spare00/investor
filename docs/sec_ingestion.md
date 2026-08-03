# SEC Ingestion

- Fixture provider for offline/tests.
- `SecEdgarAdapter` uses SEC company tickers + submissions JSON with required User-Agent and conservative timeouts.
- Stores metadata + document URL reference; full filing bodies are not dumped into LLM context.
- Context Builder passes form type, accession, hints only.
