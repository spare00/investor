from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate unit tests from a developer's live paper .env / local Postgres.

    CI has no .env and no Postgres on :5432 — tests must not depend on either.
    """
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("ENABLE_BROKER_ORDERS", "false")
    monkeypatch.setenv("ENABLE_AUTOMATED_EXECUTION", "false")
    monkeypatch.setenv("ENABLE_EXTERNAL_DATA", "false")
    monkeypatch.setenv("ENABLE_MARKET_DATA_COLLECTION", "false")
    monkeypatch.setenv("ENABLE_BROKER_CONNECTION", "false")
    monkeypatch.setenv("BROKER_PROVIDER", "mock")
    monkeypatch.setenv("BROKER_ENVIRONMENT", "paper")
    monkeypatch.setenv(
        "DATABASE_URL", "sqlite+aiosqlite:///:memory:?cache=shared"
    )
    monkeypatch.setenv("LLM_RUNTIME", "cloud")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_LOCAL_NUM_CTX", "8192")
    monkeypatch.setenv("LLM_LOCAL_MAX_TOKENS", "800")
    monkeypatch.setenv("LLM_LOCAL_FAST_MODEL", "")
    from app.core import database as db
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    db._engine = None
    db._session_factory = None
    yield
    clear_settings_cache()
    db._engine = None
    db._session_factory = None
