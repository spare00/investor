"""Alembic revision: persist IBKR con_id on positions and lifecycles."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_position_con_id"
down_revision = "0009_pos_symbol_venue_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("con_id", sa.Integer(), nullable=True))
    op.create_index("ix_positions_con_id", "positions", ["con_id"], unique=False)
    # Partial unique when present — Postgres only; SQLite tests use create_all.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_positions_con_id_not_null "
            "ON positions (con_id) WHERE con_id IS NOT NULL"
        )

    op.add_column("position_lifecycles", sa.Column("con_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_position_lifecycles_con_id", "position_lifecycles", ["con_id"], unique=False
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_lifecycles_con_id_open "
            "ON position_lifecycles (con_id) "
            "WHERE con_id IS NOT NULL AND status IN "
            "('OPEN','PENDING_OPEN','ADDING','REDUCING','PENDING_CLOSE')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_lifecycles_con_id_open")
        op.execute("DROP INDEX IF EXISTS uq_positions_con_id_not_null")
    op.drop_index("ix_position_lifecycles_con_id", table_name="position_lifecycles")
    op.drop_column("position_lifecycles", "con_id")
    op.drop_index("ix_positions_con_id", table_name="positions")
    op.drop_column("positions", "con_id")
