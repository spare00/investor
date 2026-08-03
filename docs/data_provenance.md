# Data Provenance

Every canonical record carries `Provenance`:

provider_name, provider_record_id, raw_payload_reference, source_timestamp,
collection_timestamp, normalizer_version, schema_version, transformations_applied,
validation_result, quality_score.

Trace path: CIO → Agent report → CollectionBundle raw_payload / canonical id → provider reference.
Credentials are never stored (`redact_secrets`).
