"""Config and live-trading dual-gate tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import Settings, TradingMode, clear_settings_cache
from app.core.security import assert_paper_or_simulation, require_execution_allowed


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_live_blocked_by_default() -> None:
    s = Settings(
        trading_mode=TradingMode.PAPER,
        live_trading_enabled=False,
    )
    assert s.is_live_trading_allowed() is False
    assert require_execution_allowed(s) == TradingMode.PAPER


def test_live_requires_dual_gate() -> None:
    s = Settings(
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        live_trading_confirmation_token=SecretStr("correct-token"),
        expected_live_confirmation_token=SecretStr("correct-token"),
    )
    assert s.is_live_trading_allowed() is True


def test_live_blocked_when_token_is_default_placeholder() -> None:
    s = Settings(
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        live_trading_confirmation_token=SecretStr("CHANGE_ME_TO_A_LONG_RANDOM_SECRET"),
        expected_live_confirmation_token=SecretStr("CHANGE_ME_TO_A_LONG_RANDOM_SECRET"),
    )
    assert s.is_live_trading_allowed() is False


def test_live_flag_without_mode_stays_paper() -> None:
    s = Settings(
        trading_mode=TradingMode.PAPER,
        live_trading_enabled=True,
        live_trading_confirmation_token=SecretStr("x"),
        expected_live_confirmation_token=SecretStr("x"),
    )
    assert s.is_live_trading_allowed() is False


def test_assert_paper_raises_on_misconfigured_live() -> None:
    s = Settings(
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        live_trading_confirmation_token=SecretStr("wrong"),
        expected_live_confirmation_token=SecretStr("right"),
    )
    with pytest.raises(PermissionError):
        assert_paper_or_simulation(s)


def test_allowlist_normalized() -> None:
    s = Settings(trade_allowlist=["spy", "qqq"])
    assert s.trade_allowlist == ["SPY", "QQQ"]
