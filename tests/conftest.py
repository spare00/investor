from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate unit tests from a developer's live paper .env."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("ENABLE_BROKER_ORDERS", "false")
    monkeypatch.setenv("ENABLE_AUTOMATED_EXECUTION", "false")
    monkeypatch.setenv("ENABLE_EXTERNAL_DATA", "false")
    monkeypatch.setenv("ENABLE_MARKET_DATA_COLLECTION", "false")
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    yield
    clear_settings_cache()
