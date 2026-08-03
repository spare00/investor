"""SQLAlchemy ORM models — Phase 2 domain persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.core.database import Base

# Use JSONB on Postgres; fall back to JSON for SQLite tests.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NewsItem(Base, TimestampMixin):
    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_news_provider_external"),
        Index("ix_news_published_at", "published_at"),
        Index("ix_news_headline_hash", "headline_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    headline_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbols: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    freshness_score: Mapped[float] = mapped_column(Float, default=1.0)
    quality_score: Mapped[float] = mapped_column(Float, default=1.0)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)


class MarketSnapshot(Base, TimestampMixin):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshots_symbol_as_of", "symbol", "as_of"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    last: Mapped[float] = mapped_column(Float, nullable=False)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_volume_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_14: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi_14: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma_50: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma_200: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    premarket_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    vix: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    freshness_score: Mapped[float] = mapped_column(Float, default=1.0)
    quality_score: Mapped[float] = mapped_column(Float, default=1.0)


class MacroSnapshot(Base, TimestampMixin):
    __tablename__ = "macro_snapshots"
    __table_args__ = (Index("ix_macro_snapshots_as_of", "as_of"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    fed_funds_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpi_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    pce_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    unemployment_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    gdp_growth_q_o_q: Mapped[float | None] = mapped_column(Float, nullable=True)
    us_10y_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    us_2y_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    dxy: Mapped[float | None] = mapped_column(Float, nullable=True)
    wti_oil: Mapped[float | None] = mapped_column(Float, nullable=True)
    gold: Mapped[float | None] = mapped_column(Float, nullable=True)
    hy_credit_spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    freshness_score: Mapped[float] = mapped_column(Float, default=1.0)
    quality_score: Mapped[float] = mapped_column(Float, default=1.0)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_workflow_started", "workflow_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    workflow_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_data_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_names: Mapped[list[Any]] = mapped_column(JSONType, default=list)

    reports: Mapped[list[AgentReport]] = relationship(back_populates="agent_run")


class AgentReport(Base, TimestampMixin):
    __tablename__ = "agent_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    data_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    agent_run: Mapped[AgentRun] = relationship(back_populates="reports")


class CIODecisionRecord(Base, TimestampMixin):
    __tablename__ = "cio_decisions"
    __table_args__ = (Index("ix_cio_decisions_timestamp", "decision_timestamp"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), unique=True, nullable=False, default=_uuid
    )
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decision_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_regime: Mapped[str] = mapped_column(String(32), nullable=False)
    portfolio_action: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    risk_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_conditions: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    reason_not_to_trade: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_data_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    prompt_version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class RiskCheck(Base, TimestampMixin):
    __tablename__ = "risk_checks"
    __table_args__ = (Index("ix_risk_checks_workflow", "workflow_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    halt_day: Mapped[bool] = mapped_column(Boolean, default=False)
    hard_vetoes: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    checks: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    adjusted_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradeSignal(Base, TimestampMixin):
    __tablename__ = "trade_signals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_position_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
        Index("ix_orders_symbol_status", "symbol", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    executions: Mapped[list[Execution]] = relationship(back_populates="order")


class Execution(Base, TimestampMixin):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    broker_execution_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    order: Mapped[Order] = relationship(back_populates="executions")


class Position(Base, TimestampMixin):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("symbol", name="uq_positions_symbol"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_basis: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sector: Mapped[str] = mapped_column(String(64), default="Unknown")
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PortfolioSnapshot(Base, TimestampMixin):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (Index("ix_portfolio_snapshots_as_of", "as_of"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    cash_pct: Mapped[float] = mapped_column(Float, nullable=False)
    gross_exposure_pct: Mapped[float] = mapped_column(Float, nullable=False)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    peak_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class DailyPerformance(Base, TimestampMixin):
    __tablename__ = "daily_performance"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_daily_performance_date"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    trade_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD ET
    starting_equity: Mapped[float] = mapped_column(Float, nullable=False)
    ending_equity: Mapped[float] = mapped_column(Float, nullable=False)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class PostTradeReview(Base, TimestampMixin):
    __tablename__ = "post_trade_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    trade_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decision_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    what_went_well: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    what_went_wrong: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    lessons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SystemEvent(Base, TimestampMixin):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_level_created", "level", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    level: Mapped[str] = mapped_column(String(16), nullable=False)  # info|warning|error
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class ConfigurationHistory(Base, TimestampMixin):
    __tablename__ = "configuration_history"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[str] = mapped_column(String(64), default="system")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class DailyWorkflowRun(Base, TimestampMixin):
    __tablename__ = "daily_workflow_runs"
    __table_args__ = (UniqueConstraint("session_date", "calendar_name", name="uq_daily_wf_session"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    session_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD ET
    calendar_name: Mapped[str] = mapped_column(String(32), nullable=False, default="NYSE")
    current_state: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    market_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    early_close: Mapped[bool] = mapped_column(Boolean, default=False)
    analysis_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    latest_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)
    resume_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intraday_reanalysis_count: Mapped[int] = mapped_column(Integer, default=0)
    revalidation_count: Mapped[int] = mapped_column(Integer, default=0)


class WorkflowStateTransition(Base, TimestampMixin):
    __tablename__ = "workflow_state_transitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("daily_workflow_runs.id"), nullable=False, index=True
    )
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)


class WorkflowLease(Base, TimestampMixin):
    __tablename__ = "workflow_leases"
    __table_args__ = (UniqueConstraint("lease_key", name="uq_workflow_lease_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    lease_key: Mapped[str] = mapped_column(String(128), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)


class ScheduledJobRecord(Base, TimestampMixin):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        UniqueConstraint("job_key", "session_date", name="uq_scheduled_job_key_session"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    job_key: Mapped[str] = mapped_column(String(128), nullable=False)
    session_date: Mapped[str] = mapped_column(String(10), nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)


class RevalidationRun(Base, TimestampMixin):
    __tablename__ = "revalidation_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("daily_workflow_runs.id"), nullable=False, index=True
    )
    result: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class DataCollectionRun(Base, TimestampMixin):
    __tablename__ = "data_collection_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    collection_type: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    providers_requested: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    providers_succeeded: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    providers_failed: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    records_received: Mapped[int] = mapped_column(Integer, default=0)
    records_normalized: Mapped[int] = mapped_column(Integer, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)
    quality_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class MarketEventRecord(Base, TimestampMixin):
    __tablename__ = "market_events"
    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uq_market_event_dedupe"),
        Index("ix_market_events_type_detected", "event_type", "detected_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    importance: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    symbols: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    sectors: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    source_record_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    trigger_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_reanalysis: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_risk_review: Mapped[bool] = mapped_column(Boolean, default=False)
    deduplication_key: Mapped[str] = mapped_column(String(256), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collection_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class DataConflictRecord(Base, TimestampMixin):
    __tablename__ = "data_conflicts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    data_type: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol_or_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    collection_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class OrderIntent(Base, TimestampMixin):
    __tablename__ = "order_intents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    intent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    approved_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    risk_check_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_policy: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)


class OrderApproval(Base, TimestampMixin):
    __tablename__ = "order_approvals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    intent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("order_intents.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING_APPROVAL")
    acted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PretradeRiskCheck(Base, TimestampMixin):
    __tablename__ = "pretrade_risk_checks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    intent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("order_intents.id"), nullable=False, index=True
    )
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class BrokerReconciliationRun(Base, TimestampMixin):
    __tablename__ = "broker_reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    sync_type: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(64), nullable=False)
    issues: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class IntradayEvent(Base, TimestampMixin):
    __tablename__ = "intraday_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    intent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    symbols: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    importance: Mapped[str] = mapped_column(String(32), default="medium")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deduplication_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    requires_analysis: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_risk_review: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_execution_review: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="NEW")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    bypass_cooldown: Mapped[bool] = mapped_column(Boolean, default=False)


class BrokerOrderEvent(Base, TimestampMixin):
    __tablename__ = "broker_order_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PositionLifecycle(Base, TimestampMixin):
    __tablename__ = "position_lifecycles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING_OPEN")
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_targets: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    take_profit_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_take_profit_indices: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    invalidation_state: Mapped[str] = mapped_column(String(32), default="NOT_TRIGGERED")
    max_holding_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overnight_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    closing_policy: Mapped[str] = mapped_column(String(64), default="CLOSE_INTRADAY_ONLY")
    protection_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    last_monitor_verdict: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    strategy_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_policy: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict)


class PositionSnapshotRecord(Base, TimestampMixin):
    __tablename__ = "position_snapshots_v2"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    position_lifecycle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("position_lifecycles.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    market_value: Mapped[float] = mapped_column(Float, nullable=False)
    average_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pl: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pl: Mapped[float | None] = mapped_column(Float, nullable=True)
    portfolio_weight_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sector_weight_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_to_stop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    holding_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="monitor")
    source_snapshot_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)


class PositionRiskReview(Base, TimestampMixin):
    __tablename__ = "position_risk_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    position_lifecycle_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class StopEvent(Base, TimestampMixin):
    __tablename__ = "stop_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    position_lifecycle_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class TakeProfitEvent(Base, TimestampMixin):
    __tablename__ = "take_profit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    position_lifecycle_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    target_index: Mapped[int] = mapped_column(Integer, nullable=False)
    target_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class IntradayAnalysisRun(Base, TimestampMixin):
    __tablename__ = "intraday_analysis_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_event_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class IntradayDecisionRecord(Base, TimestampMixin):
    __tablename__ = "intraday_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    parent_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    trigger_event_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_regime: Mapped[str] = mapped_column(String(32), nullable=False)
    thesis_status: Mapped[str] = mapped_column(String(32), nullable=False)
    portfolio_action: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol_actions: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    risk_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_conditions: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    dissenting_views: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    decision_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class ClosingReview(Base, TimestampMixin):
    __tablename__ = "closing_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    policy: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    intent_drafts: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    notes: Mapped[list[Any]] = mapped_column(JSONType, default=list)


class OvernightReview(Base, TimestampMixin):
    __tablename__ = "overnight_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    position_lifecycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    valid_for_session_date: Mapped[str] = mapped_column(String(10), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PostmarketSettlement(Base, TimestampMixin):
    __tablename__ = "postmarket_settlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    session_date: Mapped[str] = mapped_column(String(10), nullable=False)
    reconciliation_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    execution_count: Mapped[int] = mapped_column(Integer, default=0)
    overnight_positions: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    pnl_summary: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class TradePnL(Base, TimestampMixin):
    __tablename__ = "trade_pnl"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    gross_realized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    net_realized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_slippage: Mapped[float] = mapped_column(Float, default=0.0)
    return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(16), default="FIFO")
    conflict_with_broker: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PostTradeReviewRecord(Base, TimestampMixin):
    __tablename__ = "posttrade_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    position_lifecycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execution_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_adherence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thesis_accuracy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timing_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    what_worked: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    what_failed: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    avoidable_errors: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    unavoidable_factors: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    lessons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    agent_assessment_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class AgentOutcomeEvaluation(Base, TimestampMixin):
    __tablename__ = "agent_outcome_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    prediction_horizon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    directional_view: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    key_claims: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    invalidation_conditions: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    actual_outcome_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class IntradayRecoveryRun(Base, TimestampMixin):
    __tablename__ = "intraday_recovery_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    new_orders_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    actions: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

