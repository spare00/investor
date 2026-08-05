"""Alembic revision: AI-managed watchlist / focus set."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_universe_watchlist"
down_revision = "0006_phase7_performance_ops"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _ts() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "watchlist_symbols",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("thesis", sa.Text(), nullable=False, server_default=""),
        sa.Column("invalidation", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(32), nullable=False, server_default="seed"),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", JSONType, nullable=False, server_default=sa.text("'{}'")),
        *_ts(),
        sa.UniqueConstraint("symbol", name="uq_watchlist_symbol"),
    )
    op.create_index("ix_watchlist_horizon_status", "watchlist_symbols", ["horizon", "status"])

    op.create_table(
        "focus_set_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.String(10), nullable=False),
        sa.Column("symbols", JSONType, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("holdings", JSONType, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(32), nullable=False, server_default="universe_service"),
        sa.Column("payload", JSONType, nullable=False, server_default=sa.text("'{}'")),
        *_ts(),
    )
    op.create_index("ix_focus_set_as_of", "focus_set_snapshots", ["as_of"])


def downgrade() -> None:
    op.drop_index("ix_focus_set_as_of", table_name="focus_set_snapshots")
    op.drop_table("focus_set_snapshots")
    op.drop_index("ix_watchlist_horizon_status", table_name="watchlist_symbols")
    op.drop_table("watchlist_symbols")
