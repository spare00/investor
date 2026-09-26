"""Repair lifecycles whose stored mark was a sign-flipped or sentinel price.

A negative mark (e.g. BHP -60.70 against a 60.56 entry) reached
``position_lifecycles.current_price`` unguarded. ``stamp_lifecycle_close_pnl``
then fell back to the unrealized number that bad mark produced, so two AU rows
were closed at +96,765 and +96,700 of fabricated profit — together larger than
every other realized P&L in the book combined, which inverted the sign of every
portfolio metric derived from it.

The true exit price is not recoverable (the affected rows carry at most one
snapshot, itself holding the flipped print), so P&L is zeroed and tagged rather
than guessed. The mark itself is restored to its magnitude, which matches the
entry price closely enough to be the real print.

Guarded at write time by ``app.intraday.pnl.usable_mark``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_repair_sign_flipped_marks"
down_revision = "0012_trade_pnl_method_len"
branch_labels = None
depends_on = None

# Mirrors app.brokers.venue_orders._MAX_SANE_EQUITY_PX.
MAX_SANE_PX = 1_000_000.0


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # jsonb concatenation below is Postgres-only; other dialects are
        # created from metadata and never carried the corrupt rows.
        return
    params = {"max_px": MAX_SANE_PX}

    # 1. Zero and tag P&L that was derived from an untrustworthy mark.
    bind.execute(
        sa.text(
            """
            UPDATE position_lifecycles
               SET realized_pl = 0,
                   unrealized_pl = NULL,
                   metadata = COALESCE(metadata, '{}'::jsonb)
                              || '{"pnl_unavailable": "no_usable_mark"}'::jsonb
             WHERE current_price IS NOT NULL
               AND (current_price <= 0 OR current_price >= :max_px)
            """
        ),
        params,
    )

    # 2. Restore the mark to its magnitude so price history is not poisoned.
    #    Sentinels (0 / DBL_MAX) carry no magnitude, so they become NULL.
    bind.execute(
        sa.text(
            """
            UPDATE position_lifecycles
               SET current_price = CASE WHEN current_price < 0 THEN -current_price END
             WHERE current_price IS NOT NULL
               AND (current_price <= 0 OR current_price >= :max_px)
            """
        ),
        params,
    )

    # 3. Same treatment for the snapshot series the peak/trough trail reads.
    bind.execute(
        sa.text(
            """
            UPDATE position_snapshots_v2
               SET current_price = CASE WHEN current_price < 0 THEN -current_price END,
                   unrealized_pl = NULL,
                   unrealized_pl_pct = NULL,
                   data_quality = 0.0
             WHERE current_price IS NOT NULL
               AND (current_price <= 0 OR current_price >= :max_px)
            """
        ),
        params,
    )


def downgrade() -> None:
    # The original values were corrupt; restoring them is not desirable.
    pass
