"""Phase 2 initial schema.

Revision ID: 0001_phase2_schema
Revises:
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_phase2_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "news_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("headline_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbols", JSONType, nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("raw_payload", JSONType, nullable=True),
        sa.Column("freshness_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("is_duplicate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "external_id", name="uq_news_provider_external"),
    )
    op.create_index("ix_news_published_at", "news_items", ["published_at"])
    op.create_index("ix_news_headline_hash", "news_items", ["headline_hash"])

    op.create_table(
        "market_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("last", sa.Float(), nullable=False),
        sa.Column("open", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("avg_volume_20d", sa.Float(), nullable=True),
        sa.Column("atr_14", sa.Float(), nullable=True),
        sa.Column("rsi_14", sa.Float(), nullable=True),
        sa.Column("sma_20", sa.Float(), nullable=True),
        sa.Column("sma_50", sa.Float(), nullable=True),
        sa.Column("sma_200", sa.Float(), nullable=True),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("spread_bps", sa.Float(), nullable=True),
        sa.Column("premarket_change_pct", sa.Float(), nullable=True),
        sa.Column("gap_pct", sa.Float(), nullable=True),
        sa.Column("vix", sa.Float(), nullable=True),
        sa.Column("raw_payload", JSONType, nullable=True),
        sa.Column("freshness_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_market_snapshots_symbol", "market_snapshots", ["symbol"])
    op.create_index("ix_market_snapshots_symbol_as_of", "market_snapshots", ["symbol", "as_of"])

    op.create_table(
        "macro_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("fed_funds_rate", sa.Float(), nullable=True),
        sa.Column("cpi_yoy", sa.Float(), nullable=True),
        sa.Column("pce_yoy", sa.Float(), nullable=True),
        sa.Column("unemployment_rate", sa.Float(), nullable=True),
        sa.Column("gdp_growth_q_o_q", sa.Float(), nullable=True),
        sa.Column("us_10y_yield", sa.Float(), nullable=True),
        sa.Column("us_2y_yield", sa.Float(), nullable=True),
        sa.Column("dxy", sa.Float(), nullable=True),
        sa.Column("wti_oil", sa.Float(), nullable=True),
        sa.Column("gold", sa.Float(), nullable=True),
        sa.Column("hy_credit_spread_bps", sa.Float(), nullable=True),
        sa.Column("notes", JSONType, nullable=True),
        sa.Column("raw_payload", JSONType, nullable=True),
        sa.Column("freshness_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("quality_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_macro_snapshots_as_of", "macro_snapshots", ["as_of"])

    op.create_table(
        "agent_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workflow_id", UUID, nullable=False),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("model_parameters", JSONType, nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("source_data_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_names", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_runs_workflow_id", "agent_runs", ["workflow_id"])
    op.create_index("ix_agent_runs_workflow_started", "agent_runs", ["workflow_id", "started_at"])

    op.create_table(
        "agent_reports",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("agent_run_id", UUID, sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("report_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("data_quality_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_reports_agent_run_id", "agent_reports", ["agent_run_id"])

    op.create_table(
        "cio_decisions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("decision_id", UUID, nullable=False, unique=True),
        sa.Column("workflow_id", UUID, nullable=True),
        sa.Column("decision_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_regime", sa.String(32), nullable=False),
        sa.Column("portfolio_action", sa.String(32), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("risk_approval", sa.Boolean(), nullable=False),
        sa.Column("risk_conditions", JSONType, nullable=True),
        sa.Column("reason_not_to_trade", sa.Text(), nullable=True),
        sa.Column("source_data_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("model_parameters", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_cio_decisions_timestamp", "cio_decisions", ["decision_timestamp"])

    op.create_table(
        "risk_checks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("workflow_id", UUID, nullable=True),
        sa.Column("decision_id", UUID, nullable=True),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("halt_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("hard_vetoes", JSONType, nullable=True),
        sa.Column("checks", JSONType, nullable=True),
        sa.Column("adjusted_quantity", sa.Float(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_risk_checks_workflow", "risk_checks", ["workflow_id"])

    op.create_table(
        "trade_signals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("decision_id", UUID, nullable=True),
        sa.Column("workflow_id", UUID, nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("target_position_pct", sa.Float(), nullable=True),
        sa.Column("payload", JSONType, nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_trade_signals_symbol", "trade_signals", ["symbol"])

    op.create_table(
        "orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("broker_order_id", sa.String(128), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("order_type", sa.String(32), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("decision_id", UUID, nullable=True),
        sa.Column("signal_id", UUID, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
    )
    op.create_index("ix_orders_broker_order_id", "orders", ["broker_order_id"])
    op.create_index("ix_orders_symbol_status", "orders", ["symbol", "status"])

    op.create_table(
        "executions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("order_id", UUID, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("broker_execution_id", sa.String(128), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_executions_order_id", "executions", ["order_id"])

    op.create_table(
        "positions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("avg_entry_price", sa.Float(), nullable=False),
        sa.Column("market_value", sa.Float(), nullable=False),
        sa.Column("cost_basis", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("sector", sa.String(64), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", name="uq_positions_symbol"),
    )

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("cash_pct", sa.Float(), nullable=False),
        sa.Column("gross_exposure_pct", sa.Float(), nullable=False),
        sa.Column("daily_pnl", sa.Float(), nullable=False),
        sa.Column("daily_pnl_pct", sa.Float(), nullable=False),
        sa.Column("drawdown_pct", sa.Float(), nullable=False),
        sa.Column("peak_equity", sa.Float(), nullable=True),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.Column("payload", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_portfolio_snapshots_as_of", "portfolio_snapshots", ["as_of"])

    op.create_table(
        "daily_performance",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("trade_date", sa.String(10), nullable=False),
        sa.Column("starting_equity", sa.Float(), nullable=False),
        sa.Column("ending_equity", sa.Float(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=False),
        sa.Column("pnl_pct", sa.Float(), nullable=False),
        sa.Column("trades_count", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("losses", sa.Integer(), nullable=False),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("trade_date", name="uq_daily_performance_date"),
    )

    op.create_table(
        "post_trade_reviews",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("trade_date", sa.String(10), nullable=False),
        sa.Column("workflow_id", UUID, nullable=True),
        sa.Column("decision_quality_score", sa.Float(), nullable=True),
        sa.Column("what_went_well", JSONType, nullable=True),
        sa.Column("what_went_wrong", JSONType, nullable=True),
        sa.Column("lessons", JSONType, nullable=True),
        sa.Column("payload", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_post_trade_reviews_trade_date", "post_trade_reviews", ["trade_date"])

    op.create_table(
        "system_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", JSONType, nullable=True),
        sa.Column("workflow_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_system_events_level_created", "system_events", ["level", "created_at"])

    op.create_table(
        "configuration_history",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_configuration_history_key", "configuration_history", ["key"])


def downgrade() -> None:
    for table in [
        "configuration_history",
        "system_events",
        "post_trade_reviews",
        "daily_performance",
        "portfolio_snapshots",
        "positions",
        "executions",
        "orders",
        "trade_signals",
        "risk_checks",
        "cio_decisions",
        "agent_reports",
        "agent_runs",
        "macro_snapshots",
        "market_snapshots",
        "news_items",
    ]:
        op.drop_table(table)
