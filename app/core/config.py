"""Application settings loaded from environment variables."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"
    SIMULATION = "simulation"


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Central configuration. Secrets never belong in source control."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Runtime
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"
    log_format: str = "json"  # json | console
    tz: str = "UTC"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:8000"]
    )

    # Database / Redis
    database_url: str = "postgresql+asyncpg://investor:investor@localhost:5432/investor"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # Trading safety
    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_enabled: bool = False
    live_trading_confirmation_token: SecretStr | None = None
    expected_live_confirmation_token: SecretStr = SecretStr(
        "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"
    )

    # Alpaca (ALPACA_SECRET_KEY accepted as alias for ALPACA_API_SECRET)
    alpaca_api_key: SecretStr | None = None
    alpaca_api_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ALPACA_API_SECRET", "ALPACA_SECRET_KEY"),
    )
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"

    # LLM
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2
    # LLM spend guard: monthly AUD is the source of truth. Daily token/call
    # budgets auto-split across trading days when set to 0.
    # OpenAI account limits remain the outer safety net.
    llm_budget_enforce: bool = True
    llm_monthly_aud_budget: float = 20.0
    # Approx US equity sessions per month used to slice the monthly AUD cap.
    llm_budget_trading_days_per_month: int = 21
    # Typical tokens/call for converting daily AUD → call cap (prompt+completion).
    llm_budget_avg_tokens_per_call: int = 5_000
    # Prompt share when inverting $/token rates (rest = completion).
    llm_budget_input_token_share: float = 0.75
    # 0 = derive from monthly AUD; >0 = explicit override (tests / manual ops).
    llm_daily_token_budget: int = 0
    llm_daily_call_budget: int = 0
    llm_aud_per_usd: float = 1.55
    llm_input_usd_per_mtok: float = 0.15  # gpt-4o-mini default
    llm_output_usd_per_mtok: float = 0.60
    llm_budget_soft_limit_pct: float = 0.8
    llm_budget_state_path: str = ".data/llm_budget_state.json"

    # Data providers
    news_provider: str = "stub"
    news_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    market_data_provider: str = "alpaca"
    yfinance_enabled: bool = False

    # Risk policy
    starting_cash: float = 25_000.0
    max_position_pct: float = 10.0
    max_sector_pct: float = 30.0
    max_gross_exposure_pct: float = 70.0
    min_cash_pct: float = 30.0
    risk_per_trade_pct: float = 0.5
    daily_max_loss_pct: float = 1.5
    max_drawdown_pct: float = 8.0
    max_open_positions: int = 8
    max_consecutive_losses: int = 3
    cooldown_after_loss_minutes: int = 30
    force_close_before_market_close_minutes: int = 15
    max_consecutive_losses_halt_day: int = 5
    min_avg_daily_volume: float = 1_000_000.0
    max_bid_ask_spread_bps: float = 20.0
    min_data_quality_score: float = 0.6
    max_slippage_bps: float = 15.0
    penny_stock_max_price: float = 5.0
    allow_leveraged_etfs: bool = False

    # Universe: static = TRADE_ALLOWLIST only; dynamic = AI watchlist (seeded from allowlist).
    universe_mode: str = "dynamic"  # static | dynamic
    universe_focus_limit: int = 12
    universe_watchlist_limit: int = 40
    universe_manager_enabled: bool = True
    # How often Universe Manager may call the LLM (days). Premarket/scheduler
    # still run between — they rebuild focus without LLM until this elapses.
    universe_refresh_min_interval_days: int = 7
    # APScheduler poll interval (seconds). Actual LLM gated by min_interval_days.
    # Default = 7d backup tick; premarket is the primary weekly opportunity.
    universe_refresh_seconds: int = 604_800
    # When true, skip periodic refresh outside premarket→after-hours (no overnight LLM).
    universe_refresh_session_only: bool = True

    # Extra symbols AI may add beyond TRADE_ALLOWLIST (empty → built-in curated pool).
    universe_candidate_pool: Annotated[list[str], NoDecode] = Field(default_factory=list)
    universe_allow_candidate_adds: bool = True
    # Liquidity screen over the curated candidate pool (not full-market).
    universe_screener_enabled: bool = True
    universe_screener_min_avg_volume: float = 1_000_000.0
    universe_screener_max_spread_bps: float = 40.0
    universe_screener_min_price: float = 5.0
    # When true, fetch live quotes; otherwise use latest DB market snapshots (+ stub fill).
    universe_screener_fetch_live: bool = False
    # Pause active watchlist names that fail the liquidity screen (never pause holdings).
    universe_screener_pause_illiquid: bool = True

    trade_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "SPY",
            "QQQ",
            "IWM",
            "DIA",
            "NVDA",
            "MSFT",
            "AMZN",
            "GOOGL",
            "META",
            "AVGO",
            "AMD",
            "AAPL",
            "TSLA",
            "IONQ",
        ]
    )

    # Intraday
    intraday_min_reeval_seconds: int = 300
    intraday_cooldown_seconds: int = 120

    # Display timezones (storage remains UTC)
    display_tz_us: str = "America/New_York"
    display_tz_local: str = "Australia/Brisbane"

    # Phase 3 market / daily workflow
    market_calendar: str = "NYSE"
    market_timezone: str = "America/New_York"
    operator_timezone: str = "Australia/Brisbane"
    premarket_preparation_minutes_before_open: int = 180
    premarket_analysis_minutes_before_open: int = 120
    preopen_revalidation_minutes_before_open: int = 10
    intraday_reevaluation_interval_minutes: int = 30
    min_reevaluation_gap_minutes: int = 15
    max_intraday_reanalyses: int = 12
    closing_window_minutes_before_close: int = 30
    postmarket_review_minutes_after_close: int = 30
    workflow_lease_seconds: int = 300
    workflow_heartbeat_seconds: int = 60
    max_revalidation_retries: int = 1
    analysis_decision_ttl_minutes: int = 180
    enable_scheduler: bool = False
    enable_automated_execution: bool = False
    enable_broker_orders: bool = False

    # Phase 5 broker / execution (safe defaults)
    broker_provider: str = "mock"  # mock | alpaca
    broker_environment: str = "paper"  # paper only in Phase 5
    enable_broker_connection: bool = False
    # Trading safety — agent firm paper path (Live always blocked)
    # REQUIRE_MANUAL_ORDER_APPROVAL is an optional ops brake, not the firm identity.
    # Default False: CIO bottom-up may auto-submit paper when ENABLE_BROKER_ORDERS +
    # ENABLE_AUTOMATED_EXECUTION are explicitly unlocked. Ship defaults keep orders off.
    require_manual_order_approval: bool = False
    enable_live_trading: bool = False  # hard block; distinct from live_trading_enabled dual-gate
    enable_short_selling: bool = False
    enable_extended_hours_orders: bool = False
    broker_request_timeout_seconds: int = 10
    broker_query_max_retries: int = 2
    broker_retry_backoff_seconds: float = 2.0
    broker_reconciliation_interval_seconds: int = 60
    order_approval_expiry_minutes: int = 10
    order_submission_revalidation_max_age_seconds: int = 15
    max_order_slippage_bps: float = 30.0
    max_order_spread_bps: float = 50.0
    # Reject limit entries whose limit is farther than this from the collected last.
    max_entry_limit_drift_bps: float = 250.0
    cancel_open_orders_at_close: bool = True
    emergency_stop_cancel_open_orders: bool = True
    emergency_stop_close_positions: bool = False
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    mock_broker_seed: int = 42

    # Phase 6 intraday operations
    intraday_operation_mode: str = "OBSERVE_ONLY"
    enable_intraday_monitoring: bool = True
    enable_intraday_agent_reanalysis: bool = True
    intraday_news_lookback_minutes: int = 90
    min_global_reanalysis_gap_minutes: int = 10
    min_symbol_reanalysis_gap_minutes: int = 10
    max_symbol_reanalyses_per_day: int = 6
    event_deduplication_window_seconds: int = 300
    position_monitor_interval_seconds: int = 60
    broker_streaming_enabled: bool = False
    broker_polling_fallback_enabled: bool = True
    broker_polling_interval_seconds: int = 30
    auto_execute_hard_stops: bool = False
    # When true and paper automation flags allow, closing window submits market exits.
    auto_execute_force_close: bool = False
    allow_stop_widening: bool = False
    allow_stop_tightening: bool = True
    allow_new_positions_in_closing_window: bool = False
    default_closing_policy: str = "CLOSE_INTRADAY_ONLY"
    cancel_entry_orders_at_closing_window: bool = True
    overnight_review_required: bool = True
    position_lot_method: str = "FIFO"

    # Phase 4 data layer (external APIs off by default)
    enable_external_data: bool = False
    enable_news_collection: bool = False
    enable_market_data_collection: bool = False
    enable_sec_collection: bool = False
    enable_macro_collection: bool = False
    sec_provider: str = "sec_edgar"
    macro_provider: str = "fixture"
    economic_calendar_provider: str = "fixture"
    market_data_provider_priority: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["fixture"]
    )
    news_provider_priority: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["fixture"]
    )
    provider_request_timeout_seconds: int = 15
    provider_max_retries: int = 2
    provider_retry_backoff_seconds: float = 2.0
    provider_circuit_breaker_failures: int = 5
    provider_circuit_breaker_reset_seconds: int = 300
    latest_quote_max_age_seconds: int = 30
    intraday_bar_max_age_seconds: int = 120
    premarket_quote_max_age_seconds: int = 120
    news_max_age_minutes: int = 1440
    economic_event_max_delay_seconds: int = 300
    account_snapshot_max_age_seconds: int = 30
    data_quality_warning_threshold: float = 0.75
    data_quality_hard_fail_threshold: float = 0.50
    quote_price_tolerance_bps: float = 20.0
    bar_volume_tolerance_pct: float = 10.0
    max_news_context_items: int = 50
    max_news_body_excerpt_chars: int = 3000
    max_sec_context_items: int = 20
    sec_user_agent: str = "InvestorBot/0.8 (contact: investor-dev@example.com)"
    calculation_version: str = "indicators_v1"

    # Phase 7 operations / observability
    primary_benchmark: str = "SPY"
    secondary_benchmark: str = "QQQ"
    risk_free_rate_annual: float = 0.0
    min_performance_observations: int = 20
    min_calibration_sample_size: int = 30
    enable_dashboard: bool = True
    dashboard_read_only: bool = True
    enable_prometheus_metrics: bool = True
    enable_alerts: bool = True
    alert_provider: str = "log"  # log | email | webhook | fake
    critical_alert_cooldown_seconds: int = 60
    warning_alert_cooldown_seconds: int = 300
    enable_fault_injection: bool = False
    enable_long_running_simulation: bool = False
    raw_provider_payload_retention_days: int = 30
    canonical_market_data_retention_days: int = 365
    audit_log_retention_days: int = 2555
    metric_retention_days: int = 1825
    performance_calculation_version: str = "perf_v1"

    # Scheduler (legacy cron placeholders; Phase 3 prefers enable_scheduler + dynamic jobs)
    scheduler_enabled: bool = False
    premarket_cron_hour: int = 8
    premarket_cron_minute: int = 0
    premarket_cron_tz: str = "America/New_York"

    @field_validator(
        "api_cors_origins",
        "trade_allowlist",
        "universe_candidate_pool",
        "market_data_provider_priority",
        "news_provider_priority",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("trade_allowlist", "universe_candidate_pool", mode="after")
    @classmethod
    def _normalize_symbols(cls, value: list[str]) -> list[str]:
        return [symbol.upper() for symbol in value]

    @model_validator(mode="after")
    def _reject_unsafe_live_default(self) -> Settings:
        """Keep live mode from being the implicit default."""
        if self.trading_mode == TradingMode.LIVE and not self.live_trading_enabled:
            # Mode alone must never activate live routing.
            object.__setattr__(self, "trading_mode", TradingMode.PAPER)
        return self

    def is_live_trading_allowed(self) -> bool:
        """Dual-gate: mode, flag, and matching confirmation token."""
        if self.trading_mode != TradingMode.LIVE:
            return False
        if not self.live_trading_enabled:
            return False
        provided = self.live_trading_confirmation_token
        expected = self.expected_live_confirmation_token
        if provided is None:
            return False
        token = provided.get_secret_value()
        expected_token = expected.get_secret_value()
        if not token or not expected_token:
            return False
        if expected_token == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET":
            return False
        return token == expected_token

    def allowlist_set(self) -> set[str]:
        return set(self.trade_allowlist)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
