# Data Quality & Freshness

- Freshness states: FRESH / ACCEPTABLE / STALE / EXPIRED / UNKNOWN (session-aware).
- Quality breakdown: overall + freshness, completeness, source_reliability, cross_provider_agreement, validation, issues.
- Thresholds: `DATA_QUALITY_WARNING_THRESHOLD`, `DATA_QUALITY_HARD_FAIL_THRESHOLD`.
- Conflicts: AGREED / MINOR_DIFFERENCE / MATERIAL_CONFLICT / UNRESOLVED / SINGLE_SOURCE_ONLY.
- Hard fail / material conflict → NO_TRADE on revalidation / analysis metadata.
