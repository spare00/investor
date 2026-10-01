"""Store one method trial per position so later edits cannot relabel it.

Entry columns are the technique at open. Exit columns are the fill, the
hold, the gross result, whether the fee was known, and whether the target
printed before the stop. Older closes are not backfilled into this table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_method_trials"
down_revision = "0013_repair_sign_flipped_marks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "method_trials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("position_lifecycle_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("venue", sa.String(8), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("strategy_id", sa.String(128), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("horizon", sa.String(16), nullable=True),
        sa.Column("entry_reason", sa.String(64), nullable=True),
        sa.Column("entry_source", sa.String(64), nullable=True),
        sa.Column("trend_at_entry", sa.String(64), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("score_kind", sa.String(32), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("target_price", sa.Float(), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("intended_hold_minutes", sa.Integer(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_captured_at_open", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("holding_minutes", sa.Float(), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("intended_order_type", sa.String(32), nullable=True),
        sa.Column("broker_order_type", sa.String(32), nullable=True),
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("fee", sa.Float(), nullable=True),
        sa.Column("fees_known", sa.Boolean(), nullable=False),
        sa.Column("path", sa.String(32), nullable=True),
        sa.Column("execution_verdict", sa.String(32), nullable=True),
        sa.Column("strategy_verdict", sa.String(32), nullable=True),
        sa.Column("cause", sa.String(64), nullable=True),
        sa.Column("counts_for_method", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["position_lifecycle_id"], ["position_lifecycles.id"]),
        sa.UniqueConstraint("position_lifecycle_id", name="uq_method_trials_lifecycle"),
    )
    op.create_index("ix_method_trials_symbol", "method_trials", ["symbol"])
    op.create_index("ix_method_trials_strategy_status", "method_trials", ["strategy_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_method_trials_strategy_status", table_name="method_trials")
    op.drop_index("ix_method_trials_symbol", table_name="method_trials")
    op.drop_table("method_trials")
