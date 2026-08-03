"""Security helpers — live trading gates and fail-closed checks."""

from __future__ import annotations

from app.core.config import Settings, TradingMode, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def assert_paper_or_simulation(settings: Settings | None = None) -> None:
    """Raise if the process would attempt live routing without dual gate."""
    cfg = settings or get_settings()
    if cfg.is_live_trading_allowed():
        logger.warning(
            "live_trading_gate_open",
            trading_mode=cfg.trading_mode.value,
            message="Live trading dual-gate passed — use extreme caution",
        )
        return
    if cfg.trading_mode == TradingMode.LIVE or cfg.live_trading_enabled:
        logger.error(
            "live_trading_blocked",
            trading_mode=cfg.trading_mode.value,
            live_flag=cfg.live_trading_enabled,
            reason="dual_gate_failed",
        )
        raise PermissionError(
            "Live trading is not allowed: dual confirmation gate failed. "
            "System remains in fail-closed / paper mode."
        )


def require_execution_allowed(settings: Settings | None = None) -> TradingMode:
    """
    Return the effective execution mode.

    Live is only returned when dual-gate passes; otherwise paper/simulation.
    """
    cfg = settings or get_settings()
    if cfg.is_live_trading_allowed():
        return TradingMode.LIVE
    if cfg.trading_mode == TradingMode.SIMULATION:
        return TradingMode.SIMULATION
    return TradingMode.PAPER
