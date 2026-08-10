"""Alembic revision: positions unique on (symbol, venue)."""

from __future__ import annotations

from alembic import op

revision = "0009_pos_symbol_venue_uq"
down_revision = "0008_position_venue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("positions") as batch:
            batch.drop_constraint("uq_positions_symbol", type_="unique")
            batch.create_unique_constraint(
                "uq_positions_symbol_venue", ["symbol", "venue"]
            )
    else:
        op.drop_constraint("uq_positions_symbol", "positions", type_="unique")
        op.create_unique_constraint(
            "uq_positions_symbol_venue", "positions", ["symbol", "venue"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("positions") as batch:
            batch.drop_constraint("uq_positions_symbol_venue", type_="unique")
            batch.create_unique_constraint("uq_positions_symbol", ["symbol"])
    else:
        op.drop_constraint("uq_positions_symbol_venue", "positions", type_="unique")
        op.create_unique_constraint("uq_positions_symbol", "positions", ["symbol"])
