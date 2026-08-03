"""Alembic revision: Phase 4 data collection tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_phase4_data_layer"
down_revision = "0002_phase3_daily_workflow"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "data_collection_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("collection_type", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("providers_requested", JSONType, nullable=False),
        sa.Column("providers_succeeded", JSONType, nullable=False),
        sa.Column("providers_failed", JSONType, nullable=False),
        sa.Column("records_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_normalized", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_summary", JSONType, nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_data_collection_runs_wf", "data_collection_runs", ["workflow_run_id"])
    op.create_table(
        "market_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("importance", sa.String(32), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("symbols", JSONType, nullable=False),
        sa.Column("sectors", JSONType, nullable=False),
        sa.Column("source_record_ids", JSONType, nullable=False),
        sa.Column("trigger_reason", sa.Text(), nullable=True),
        sa.Column("requires_reanalysis", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requires_risk_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deduplication_key", sa.String(256), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collection_run_id", sa.Uuid(), nullable=True),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("deduplication_key", name="uq_market_event_dedupe"),
    )
    op.create_index("ix_market_events_type_detected", "market_events", ["event_type", "detected_at"])
    op.create_table(
        "data_conflicts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("data_type", sa.String(64), nullable=False),
        sa.Column("symbol_or_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("details", JSONType, nullable=False),
        sa.Column("collection_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_data_conflicts_symbol", "data_conflicts", ["symbol_or_key"])


def downgrade() -> None:
    op.drop_index("ix_data_conflicts_symbol", table_name="data_conflicts")
    op.drop_table("data_conflicts")
    op.drop_index("ix_market_events_type_detected", table_name="market_events")
    op.drop_table("market_events")
    op.drop_index("ix_data_collection_runs_wf", table_name="data_collection_runs")
    op.drop_table("data_collection_runs")
