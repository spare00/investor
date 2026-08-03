"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.analysis import router as analysis_router
from app.api.collection import router as collection_router
from app.api.dashboard import router as dashboard_router
from app.api.portfolio import router as portfolio_router
from app.api.trading import router as trading_router
from app.api.workflow import router as workflow_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.metrics import TRADING_STATE, trading_state_value
from app.core.scheduler import start_scheduler, stop_scheduler, upcoming_jobs
from app.core.security import require_execution_allowed
from app.execution.safety_controls import trading_controls

logger = get_logger(__name__)
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    mode = require_execution_allowed(settings)
    start_scheduler(settings)
    TRADING_STATE.set(trading_state_value(trading_controls.snapshot().state.value))
    logger.info(
        "app_startup",
        app_env=settings.app_env.value,
        trading_mode=mode.value,
        live_allowed=settings.is_live_trading_allowed(),
        alpaca_configured=bool(settings.alpaca_api_key and settings.alpaca_api_secret),
        allowlist_size=len(settings.trade_allowlist),
        phase=7,
    )
    yield
    await stop_scheduler()
    logger.info("app_shutdown")


app = FastAPI(
    title="Investor",
    description="Six-agent AI investment firm (paper trading first)",
    version="0.7.0",
    lifespan=lifespan,
)
app.include_router(collection_router)
app.include_router(analysis_router)
app.include_router(trading_router)
app.include_router(workflow_router)
app.include_router(portfolio_router)
app.include_router(dashboard_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
async def dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": "0.7.0",
        "trading_mode": require_execution_allowed(settings).value,
        "live_trading_allowed": settings.is_live_trading_allowed(),
        "alpaca_configured": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
        "phase": 7,
    }


@app.get("/status")
async def status() -> dict[str, Any]:
    settings = get_settings()
    controls = trading_controls.snapshot()
    TRADING_STATE.set(trading_state_value(controls.state.value))
    return {
        "env": settings.app_env.value,
        "trading_mode": require_execution_allowed(settings).value,
        "live_trading_allowed": settings.is_live_trading_allowed(),
        "trading_state": controls.state.value,
        "new_orders_allowed": trading_controls.is_new_order_allowed(),
        "alpaca_configured": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
        "scheduler_enabled": settings.scheduler_enabled,
        "allowlist": settings.trade_allowlist,
        "risk": {
            "max_position_pct": settings.max_position_pct,
            "min_cash_pct": settings.min_cash_pct,
            "daily_max_loss_pct": settings.daily_max_loss_pct,
            "max_drawdown_pct": settings.max_drawdown_pct,
        },
        "next_jobs": upcoming_jobs(),
        "note": "Phase 7 dashboard and Prometheus metrics online",
        "endpoints": {
            "dashboard": "GET /dashboard",
            "metrics": "GET /metrics",
            "summary": "GET /dashboard/summary",
            "decisions": "GET /decisions",
            "agent_runs": "GET /agents/runs",
            "events": "GET /events",
            "premarket_run": "POST /workflow/premarket/run",
            "portfolio": "GET /portfolio",
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
