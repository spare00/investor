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
    intraday_reevaluation_interval_minutes: int = 20
    min_reevaluation_gap_minutes: int = 10
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

    # Scheduler (legacy cron placeholders; Phase 3 prefers enable_scheduler + dynamic jobs)
    scheduler_enabled: bool = False
    premarket_cron_hour: int = 8
    premarket_cron_minute: int = 0
    premarket_cron_tz: str = "America/New_York"

    @field_validator("api_cors_origins", "trade_allowlist", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("trade_allowlist", mode="after")
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
