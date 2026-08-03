"""Alembic revision: Phase 5 execution / broker tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_phase5_broker_execution"
down_revision = "0003_phase4_data_layer"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "order_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("intent_type", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("approved_quantity", sa.Float(), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("client_order_id", sa.String(64), nullable=True),
        sa.Column("risk_check_id", sa.Uuid(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_policy", JSONType, nullable=False),
        sa.Column("metadata", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("client_order_id", name="uq_order_intent_client_order_id"),
    )
    op.create_index("ix_order_intents_symbol", "order_intents", ["symbol"])
    op.create_table(
        "order_approvals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("intent_id", sa.Uuid(), sa.ForeignKey("order_intents.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("acted_by", sa.String(64), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "pretrade_risk_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("intent_id", sa.Uuid(), sa.ForeignKey("order_intents.id"), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "broker_reconciliation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sync_type", sa.String(32), nullable=False),
        sa.Column("result", sa.String(64), nullable=False),
        sa.Column("issues", JSONType, nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("broker_reconciliation_runs")
    op.drop_table("pretrade_risk_checks")
    op.drop_table("order_approvals")
    op.drop_index("ix_order_intents_symbol", table_name="order_intents")
    op.drop_table("order_intents")
