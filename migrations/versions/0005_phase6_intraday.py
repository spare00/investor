"""Alembic revision: Phase 6 intraday / position management."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_phase6_intraday"
down_revision = "0004_phase5_broker_execution"
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
        "intraday_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(128), nullable=True),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("intent_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("symbols", JSONType, nullable=False),
        sa.Column("importance", sa.String(32), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deduplication_key", sa.String(256), nullable=False),
        sa.Column("requires_analysis", sa.Boolean(), nullable=False),
        sa.Column("requires_risk_review", sa.Boolean(), nullable=False),
        sa.Column("requires_execution_review", sa.Boolean(), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("bypass_cooldown", sa.Boolean(), nullable=False),
        *_ts(),
    )
    op.create_index("ix_intraday_events_type", "intraday_events", ["event_type"])
    op.create_index("ix_intraday_events_dedup", "intraday_events", ["deduplication_key"])

    op.create_table(
        "broker_order_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("broker_order_id", sa.String(128), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("broker_status", sa.String(64), nullable=True),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "position_lifecycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("average_entry_price", sa.Float(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("unrealized_pl", sa.Float(), nullable=True),
        sa.Column("realized_pl", sa.Float(), nullable=False),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("stop_status", sa.String(32), nullable=True),
        sa.Column("take_profit_price", sa.Float(), nullable=True),
        sa.Column("take_profit_targets", JSONType, nullable=False),
        sa.Column("take_profit_state", sa.String(64), nullable=True),
        sa.Column("filled_take_profit_indices", JSONType, nullable=False),
        sa.Column("invalidation_state", sa.String(32), nullable=False),
        sa.Column("max_holding_minutes", sa.Integer(), nullable=True),
        sa.Column("overnight_allowed", sa.Boolean(), nullable=False),
        sa.Column("closing_policy", sa.String(64), nullable=False),
        sa.Column("protection_submitted", sa.Boolean(), nullable=False),
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("last_monitor_verdict", sa.String(64), nullable=True),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_reference", sa.String(128), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_policy", JSONType, nullable=False),
        sa.Column("metadata", JSONType, nullable=False),
        *_ts(),
    )
    op.create_index("ix_position_lifecycles_symbol", "position_lifecycles", ["symbol"])

    op.create_table(
        "position_snapshots_v2",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("position_lifecycle_id", sa.Uuid(), sa.ForeignKey("position_lifecycles.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("market_value", sa.Float(), nullable=False),
        sa.Column("average_entry_price", sa.Float(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("unrealized_pl", sa.Float(), nullable=True),
        sa.Column("unrealized_pl_pct", sa.Float(), nullable=True),
        sa.Column("realized_pl", sa.Float(), nullable=True),
        sa.Column("portfolio_weight_pct", sa.Float(), nullable=True),
        sa.Column("sector_weight_pct", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("distance_to_stop_pct", sa.Float(), nullable=True),
        sa.Column("take_profit_state", sa.String(64), nullable=True),
        sa.Column("holding_minutes", sa.Float(), nullable=True),
        sa.Column("risk_amount", sa.Float(), nullable=True),
        sa.Column("data_quality", sa.Float(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_snapshot_ids", JSONType, nullable=False),
        *_ts(),
    )

    for name, cols in [
        (
            "position_risk_reviews",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("position_lifecycle_id", sa.Uuid(), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("reasons", JSONType, nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "stop_events",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("position_lifecycle_id", sa.Uuid(), nullable=False),
                sa.Column("kind", sa.String(32), nullable=False),
                sa.Column("status", sa.String(32), nullable=False),
                sa.Column("stop_price", sa.Float(), nullable=True),
                sa.Column("trigger_price", sa.Float(), nullable=True),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "take_profit_events",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("position_lifecycle_id", sa.Uuid(), nullable=False),
                sa.Column("target_index", sa.Integer(), nullable=False),
                sa.Column("target_price", sa.Float(), nullable=False),
                sa.Column("quantity", sa.Float(), nullable=False),
                sa.Column("status", sa.String(32), nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "intraday_analysis_runs",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("status", sa.String(32), nullable=False),
                sa.Column("trigger_event_ids", JSONType, nullable=False),
                sa.Column("mode", sa.String(32), nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "intraday_decisions",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("parent_decision_id", sa.Uuid(), nullable=True),
                sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
                sa.Column("trigger_event_ids", JSONType, nullable=False),
                sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
                sa.Column("market_regime", sa.String(32), nullable=False),
                sa.Column("thesis_status", sa.String(32), nullable=False),
                sa.Column("portfolio_action", sa.String(32), nullable=False),
                sa.Column("symbol_actions", JSONType, nullable=False),
                sa.Column("risk_approval", sa.Boolean(), nullable=False),
                sa.Column("risk_conditions", JSONType, nullable=False),
                sa.Column("dissenting_views", JSONType, nullable=False),
                sa.Column("decision_expiry", sa.DateTime(timezone=True), nullable=True),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "closing_reviews",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("policy", sa.String(64), nullable=False),
                sa.Column("payload", JSONType, nullable=False),
                sa.Column("intent_drafts", JSONType, nullable=False),
                sa.Column("notes", JSONType, nullable=False),
            ],
        ),
        (
            "overnight_reviews",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("position_lifecycle_id", sa.Uuid(), nullable=True),
                sa.Column("symbol", sa.String(32), nullable=False),
                sa.Column("status", sa.String(64), nullable=False),
                sa.Column("reasons", JSONType, nullable=False),
                sa.Column("valid_for_session_date", sa.String(10), nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "postmarket_settlements",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("session_date", sa.String(10), nullable=False),
                sa.Column("reconciliation_result", sa.String(64), nullable=True),
                sa.Column("account_snapshot", JSONType, nullable=False),
                sa.Column("order_count", sa.Integer(), nullable=False),
                sa.Column("execution_count", sa.Integer(), nullable=False),
                sa.Column("overnight_positions", JSONType, nullable=False),
                sa.Column("pnl_summary", JSONType, nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "trade_pnl",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("symbol", sa.String(32), nullable=False),
                sa.Column("gross_realized_pl", sa.Float(), nullable=False),
                sa.Column("net_realized_pl", sa.Float(), nullable=False),
                sa.Column("unrealized_pl", sa.Float(), nullable=False),
                sa.Column("fees", sa.Float(), nullable=False),
                sa.Column("estimated_slippage", sa.Float(), nullable=False),
                sa.Column("return_pct", sa.Float(), nullable=False),
                sa.Column("method", sa.String(16), nullable=False),
                sa.Column("conflict_with_broker", sa.Boolean(), nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "posttrade_reviews",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("position_lifecycle_id", sa.Uuid(), nullable=True),
                sa.Column("decision_id", sa.Uuid(), nullable=True),
                sa.Column("symbol", sa.String(32), nullable=False),
                sa.Column("outcome", sa.String(64), nullable=False),
                sa.Column("exit_reason", sa.Text(), nullable=True),
                sa.Column("pnl", sa.Float(), nullable=True),
                sa.Column("entry_quality", sa.String(32), nullable=True),
                sa.Column("execution_quality", sa.String(32), nullable=True),
                sa.Column("risk_adherence", sa.String(32), nullable=True),
                sa.Column("exit_quality", sa.String(32), nullable=True),
                sa.Column("thesis_accuracy", sa.String(32), nullable=True),
                sa.Column("timing_quality", sa.String(32), nullable=True),
                sa.Column("data_quality", sa.String(32), nullable=True),
                sa.Column("what_worked", JSONType, nullable=False),
                sa.Column("what_failed", JSONType, nullable=False),
                sa.Column("avoidable_errors", JSONType, nullable=False),
                sa.Column("unavoidable_factors", JSONType, nullable=False),
                sa.Column("lessons", JSONType, nullable=False),
                sa.Column("agent_assessment_ids", JSONType, nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "agent_outcome_evaluations",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("agent_name", sa.String(64), nullable=False),
                sa.Column("agent_run_id", sa.Uuid(), nullable=True),
                sa.Column("report_id", sa.Uuid(), nullable=True),
                sa.Column("prediction_horizon", sa.String(64), nullable=True),
                sa.Column("directional_view", sa.String(64), nullable=True),
                sa.Column("confidence", sa.Float(), nullable=True),
                sa.Column("key_claims", JSONType, nullable=False),
                sa.Column("invalidation_conditions", JSONType, nullable=False),
                sa.Column("actual_outcome_reference", sa.String(128), nullable=True),
                sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
        (
            "intraday_recovery_runs",
            [
                sa.Column("id", sa.Uuid(), primary_key=True),
                sa.Column("emergency_stop", sa.Boolean(), nullable=False),
                sa.Column("new_orders_allowed", sa.Boolean(), nullable=False),
                sa.Column("actions", JSONType, nullable=False),
                sa.Column("payload", JSONType, nullable=False),
            ],
        ),
    ]:
        op.create_table(name, *cols, *_ts())


def downgrade() -> None:
    for name in [
        "intraday_recovery_runs",
        "agent_outcome_evaluations",
        "posttrade_reviews",
        "trade_pnl",
        "postmarket_settlements",
        "overnight_reviews",
        "closing_reviews",
        "intraday_decisions",
        "intraday_analysis_runs",
        "take_profit_events",
        "stop_events",
        "position_risk_reviews",
        "position_snapshots_v2",
        "position_lifecycles",
        "broker_order_events",
        "intraday_events",
    ]:
        op.drop_table(name)
