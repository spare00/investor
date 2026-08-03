# Data Retention

## Policy

`app/ops/retention.py` — `RetentionPolicy` with category targets:

| Category | Default retention | Phase 7 action |
|----------|-------------------|----------------|
| raw_provider_payload | 30 days | plan_only |
| canonical_market_data | 365 days | plan_only |
| audit_log | 2555 days (~7y) | plan_only |
| performance_metrics | 1825 days (~5y) | plan_only |

## Config

See `.env.example` — `RAW_PROVIDER_PAYLOAD_RETENTION_DAYS`, etc.

## Not supported

- Automatic deletion (`execute=True` not implemented)
- Legal hold / litigation freeze flags
- Per-table TTL cron job

Run dry-run: `RetentionPolicy(settings).plan(dry_run=True)` (see tests).
