"""Widen trade_pnl.method so session/venue tags cannot abort settlement.

The column was VARCHAR(16). Settlement used to store ``FIFO:YYYY-MM-DD:AU``
(18 chars), which Postgres rejected and left postmarket_review stuck running.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_trade_pnl_method_len"
down_revision = "0011_embedding_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "trade_pnl",
        "method",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "trade_pnl",
        "method",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
