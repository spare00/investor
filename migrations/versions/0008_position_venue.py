"""Alembic revision: venue / currency on positions and lifecycles."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_position_venue"
down_revision = "0007_universe_watchlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("venue", sa.String(8), nullable=False, server_default="US"),
    )
    op.add_column("positions", sa.Column("currency", sa.String(8), nullable=True))
    op.add_column("positions", sa.Column("exchange", sa.String(32), nullable=True))
    op.create_index("ix_positions_venue", "positions", ["venue"])

    op.add_column(
        "position_lifecycles",
        sa.Column("venue", sa.String(8), nullable=False, server_default="US"),
    )
    op.add_column("position_lifecycles", sa.Column("currency", sa.String(8), nullable=True))
    op.create_index("ix_position_lifecycles_venue", "position_lifecycles", ["venue"])


def downgrade() -> None:
    op.drop_index("ix_position_lifecycles_venue", table_name="position_lifecycles")
    op.drop_column("position_lifecycles", "currency")
    op.drop_column("position_lifecycles", "venue")
    op.drop_index("ix_positions_venue", table_name="positions")
    op.drop_column("positions", "exchange")
    op.drop_column("positions", "currency")
    op.drop_column("positions", "venue")
