"""Alembic revision: Phase 7 performance metrics and operations."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_phase7_performance_ops"
down_revision = "0005_phase6_intraday"
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
        "portfolio_valuations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("portfolio_id", sa.String(64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valuation_kind", sa.String(32), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("long_market_value", sa.Float(), nullable=False),
        sa.Column("short_market_value", sa.Float(), nullable=False),
        sa.Column("gross_exposure", sa.Float(), nullable=False),
        sa.Column("net_exposure", sa.Float(), nullable=False),
        sa.Column("portfolio_value", sa.Float(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("buying_power", sa.Float(), nullable=False),
        sa.Column("realized_pl_day", sa.Float(), nullable=False),
        sa.Column("unrealized_pl", sa.Float(), nullable=False),
        sa.Column("fees_day", sa.Float(), nullable=False),
        sa.Column("estimated_slippage_day", sa.Float(), nullable=False),
        sa.Column("net_liquidation_value", sa.Float(), nullable=False),
        sa.Column("benchmark_values", JSONType, nullable=False),
        sa.Column("source_snapshot_ids", JSONType, nullable=False),
        sa.Column("data_quality", sa.String(32), nullable=True),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        *_ts(),
        sa.UniqueConstraint("portfolio_id", "as_of", "valuation_kind", name="uq_portfolio_valuation"),
    )
    op.create_index("ix_portfolio_valuations_portfolio_as_of", "portfolio_valuations", ["portfolio_id", "as_of"])

    op.create_table(
        "portfolio_returns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("portfolio_id", sa.String(64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("excess_return", sa.Float(), nullable=True),
        sa.Column("active_return", sa.Float(), nullable=True),
        sa.Column("benchmark", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "performance_metrics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("annualization_factor", sa.Float(), nullable=True),
        sa.Column("risk_free_rate", sa.Float(), nullable=True),
        sa.Column("benchmark", sa.String(32), nullable=True),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        sa.Column("data_quality", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metric_scope", sa.String(64), nullable=False),
        sa.Column("calculation_run_id", sa.Uuid(), nullable=True),
        *_ts(),
    )

    op.create_table(
        "drawdown_periods",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("peak_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("peak_value", sa.Float(), nullable=False),
        sa.Column("trough_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trough_value", sa.Float(), nullable=False),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drawdown_pct", sa.Float(), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("recovery_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "trade_metrics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metrics", JSONType, nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        *_ts(),
    )

    op.create_table(
        "execution_quality_metrics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("metrics", JSONType, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        *_ts(),
    )

    op.create_table(
        "decision_evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("decision_type", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("decision_price", sa.Float(), nullable=False),
        sa.Column("evaluation_horizon", sa.String(32), nullable=False),
        sa.Column("price_at_horizon", sa.Float(), nullable=True),
        sa.Column("return_after_decision", sa.Float(), nullable=True),
        sa.Column("benchmark_return_after_decision", sa.Float(), nullable=True),
        sa.Column("excess_return", sa.Float(), nullable=True),
        sa.Column("max_adverse_move", sa.Float(), nullable=True),
        sa.Column("max_favorable_move", sa.Float(), nullable=True),
        sa.Column("thesis_status", sa.String(32), nullable=True),
        sa.Column("invalidation_accuracy", sa.Float(), nullable=True),
        sa.Column("action_quality", sa.String(32), nullable=True),
        sa.Column("abstention_quality", sa.String(32), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "agent_evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("prediction_horizon", sa.String(64), nullable=False),
        sa.Column("directional_view", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("key_claims", JSONType, nullable=False),
        sa.Column("risk_warnings", JSONType, nullable=False),
        sa.Column("abstained", sa.Boolean(), nullable=False),
        sa.Column("actual_outcome", sa.String(128), nullable=True),
        sa.Column("direction_correct", sa.Boolean(), nullable=True),
        sa.Column("warning_useful", sa.Boolean(), nullable=True),
        sa.Column("invalidation_correct", sa.Boolean(), nullable=True),
        sa.Column("confidence_error", sa.Float(), nullable=True),
        sa.Column("evaluation_status", sa.String(32), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "agent_calibration_metrics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(32), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("accuracy", sa.Float(), nullable=True),
        sa.Column("avg_confidence", sa.Float(), nullable=True),
        sa.Column("calibration_gap", sa.Float(), nullable=True),
        sa.Column("brier_score", sa.Float(), nullable=True),
        sa.Column("ece", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        *_ts(),
    )

    op.create_table(
        "provider_reliability_metrics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("metrics", JSONType, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        *_ts(),
    )

    op.create_table(
        "provider_incidents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("incident_type", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("affected_workflows", JSONType, nullable=False),
        sa.Column("affected_decisions", JSONType, nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("trading_blocked", sa.Boolean(), nullable=False),
        sa.Column("data_quality_impact", sa.String(32), nullable=True),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "operational_metrics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("labels", JSONType, nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        *_ts(),
    )

    op.create_table(
        "benchmark_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("total_return_index", sa.Float(), nullable=True),
        sa.Column("adjusted_close", sa.Float(), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("provenance", JSONType, nullable=False),
        sa.Column("freshness_seconds", sa.Float(), nullable=True),
        *_ts(),
    )

    op.create_table(
        "metric_calculation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("metric_scope", sa.String(64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_snapshot_ids", JSONType, nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        sa.Column("records_processed", sa.Integer(), nullable=False),
        sa.Column("records_rejected", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "operational_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("alert_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("source_event_ids", JSONType, nullable=False),
        sa.Column("deduplication_key", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scenario", sa.String(64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trading_days", sa.Integer(), nullable=False),
        sa.Column("workflow_success_rate", sa.Float(), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("benchmark_return", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("sortino", sa.Float(), nullable=True),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("profit_factor", sa.Float(), nullable=True),
        sa.Column("risk_limit_breaches", sa.Integer(), nullable=False),
        sa.Column("emergency_stops", sa.Integer(), nullable=False),
        sa.Column("recovery_count", sa.Integer(), nullable=False),
        sa.Column("agent_failures", sa.Integer(), nullable=False),
        sa.Column("provider_failures", sa.Integer(), nullable=False),
        sa.Column("code_version", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=True),
        sa.Column("configuration_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        *_ts(),
    )

    op.create_table(
        "readiness_evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("gate", sa.String(64), nullable=False),
        sa.Column("result", JSONType, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operator_note", sa.Text(), nullable=True),
        *_ts(),
    )


def downgrade() -> None:
    for table in (
        "readiness_evaluations",
        "simulation_runs",
        "operational_alerts",
        "metric_calculation_runs",
        "benchmark_snapshots",
        "operational_metrics",
        "provider_incidents",
        "provider_reliability_metrics",
        "agent_calibration_metrics",
        "agent_evaluations",
        "decision_evaluations",
        "execution_quality_metrics",
        "trade_metrics",
        "drawdown_periods",
        "performance_metrics",
        "portfolio_returns",
        "portfolio_valuations",
    ):
        op.drop_table(table)
