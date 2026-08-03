"""Alembic revision: Phase 3 daily workflow tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_phase3_daily_workflow"
down_revision = "0001_phase2_schema"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "daily_workflow_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("calendar_name", sa.String(32), nullable=False),
        sa.Column("current_state", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("market_open_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("market_close_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("early_close", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("analysis_workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("latest_decision_id", sa.Uuid(), nullable=True),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("resume_state", sa.String(64), nullable=True),
        sa.Column("intraday_reanalysis_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revalidation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("session_date", "calendar_name", name="uq_daily_wf_session"),
    )
    op.create_table(
        "workflow_state_transitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workflow_run_id", sa.Uuid(), sa.ForeignKey("daily_workflow_runs.id"), nullable=False),
        sa.Column("from_state", sa.String(64), nullable=False),
        sa.Column("to_state", sa.String(64), nullable=False),
        sa.Column("trigger", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_wf_transitions_run", "workflow_state_transitions", ["workflow_run_id"])
    op.create_table(
        "workflow_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("lease_key", sa.String(128), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("lease_key", name="uq_workflow_lease_key"),
    )
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_key", sa.String(128), nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("job_key", "session_date", name="uq_scheduled_job_key_session"),
    )
    op.create_table(
        "revalidation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workflow_run_id", sa.Uuid(), sa.ForeignKey("daily_workflow_runs.id"), nullable=False),
        sa.Column("result", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("revalidation_runs")
    op.drop_table("scheduled_jobs")
    op.drop_table("workflow_leases")
    op.drop_index("ix_wf_transitions_run", table_name="workflow_state_transitions")
    op.drop_table("workflow_state_transitions")
    op.drop_table("daily_workflow_runs")
