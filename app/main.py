"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.api.analysis import router as analysis_router
from app.api.collection import router as collection_router
from app.api.trading import router as trading_router
from app.api.workflow import router as workflow_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.scheduler import start_scheduler, stop_scheduler, upcoming_jobs
from app.core.security import require_execution_allowed
from app.execution.safety_controls import trading_controls

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    mode = require_execution_allowed(settings)
    start_scheduler(settings)
    logger.info(
        "app_startup",
        app_env=settings.app_env.value,
        trading_mode=mode.value,
        live_allowed=settings.is_live_trading_allowed(),
        allowlist_size=len(settings.trade_allowlist),
        phase=5,
    )
    yield
    await stop_scheduler()
    logger.info("app_shutdown")


app = FastAPI(
    title="Investor",
    description="Six-agent AI investment firm (paper trading first)",
    version="0.5.0",
    lifespan=lifespan,
)
app.include_router(collection_router)
app.include_router(analysis_router)
app.include_router(trading_router)
app.include_router(workflow_router)


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": "0.5.0",
        "trading_mode": require_execution_allowed(settings).value,
        "live_trading_allowed": settings.is_live_trading_allowed(),
        "phase": 5,
    }


@app.get("/status")
async def status() -> dict[str, Any]:
    settings = get_settings()
    controls = trading_controls.snapshot()
    return {
        "env": settings.app_env.value,
        "trading_mode": require_execution_allowed(settings).value,
        "live_trading_allowed": settings.is_live_trading_allowed(),
        "trading_state": controls.state.value,
        "new_orders_allowed": trading_controls.is_new_order_allowed(),
        "scheduler_enabled": settings.scheduler_enabled,
        "allowlist": settings.trade_allowlist,
        "risk": {
            "max_position_pct": settings.max_position_pct,
            "min_cash_pct": settings.min_cash_pct,
            "daily_max_loss_pct": settings.daily_max_loss_pct,
            "max_drawdown_pct": settings.max_drawdown_pct,
        },
        "next_jobs": upcoming_jobs(),
        "note": "Phase 5 workflows online — paper order submit deferred to Phase 6",
        "endpoints": {
            "premarket_run": "POST /workflow/premarket/run",
            "intraday_evaluate": "POST /workflow/intraday/evaluate",
            "postmarket_run": "POST /workflow/postmarket/run",
            "trading_pause": "POST /trading/pause",
            "emergency_stop": "POST /trading/emergency-stop",
        },
    }


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_env.value == "development",
    )


if __name__ == "__main__":
    run()
