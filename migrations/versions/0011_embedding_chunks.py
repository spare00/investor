"""Alembic revision: embedding_chunks for RAG / token reduction."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_embedding_chunks"
down_revision = "0010_position_con_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    json_type = postgresql.JSONB() if bind.dialect.name == "postgresql" else sa.JSON()
    op.create_table(
        "embedding_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("venue", sa.String(8), nullable=True),
        sa.Column("horizon", sa.String(16), nullable=True),
        sa.Column("symbols", json_type, nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", json_type, nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_type", "source_id", name="uq_embedding_source"),
    )
    op.create_index("ix_embedding_as_of", "embedding_chunks", ["as_of"])
    op.create_index("ix_embedding_venue_horizon", "embedding_chunks", ["venue", "horizon"])
    op.create_index("ix_embedding_content_hash", "embedding_chunks", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_embedding_content_hash", table_name="embedding_chunks")
    op.drop_index("ix_embedding_venue_horizon", table_name="embedding_chunks")
    op.drop_index("ix_embedding_as_of", table_name="embedding_chunks")
    op.drop_table("embedding_chunks")
