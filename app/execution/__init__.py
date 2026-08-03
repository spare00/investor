"""Execution package — validation and safety (orders in Phase 6)."""

# Keep imports lazy to avoid circular import deadlocks during package init.
__all__ = [
    "ExecutionValidationResult",
    "ExecutionValidator",
    "TradingControls",
    "TradingState",
    "trading_controls",
]


def __getattr__(name: str):
    if name in {"TradingControls", "TradingState", "trading_controls"}:
        from app.execution import safety_controls as sc

        return getattr(sc, name)
    if name in {"ExecutionValidationResult", "ExecutionValidator"}:
        from app.execution import validation as v

        return getattr(v, name)
    raise AttributeError(name)
