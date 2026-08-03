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
from app.api.broker import router as broker_router
from app.api.collection import router as collection_router
from app.api.daily_workflow import router as daily_workflow_router
from app.api.dashboard import router as dashboard_router
from app.api.data import router as data_router
from app.api.execution import router as execution_router
from app.api.market import router as market_router
from app.api.portfolio import router as portfolio_router
from app.api.trading import router as trading_router
from app.api.workflow import router as workflow_router
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger, setup_logging
from app.core.metrics import TRADING_STATE, trading_state_value
from app.core.scheduler import start_scheduler, stop_scheduler, upcoming_jobs
from app.core.security import require_execution_allowed
from app.execution.safety_controls import trading_controls
from app.workflow.recovery import RecoveryService

logger = get_logger(__name__)
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    mode = require_execution_allowed(settings)
    try:
        factory = get_session_factory()
        async with factory() as session:
            recovery = await RecoveryService(session).run()
            await session.commit()
            logger.info("startup_recovery", **{k: recovery[k] for k in ("reclaimed_leases", "emergency_stop")})
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup_recovery_skipped", error=str(exc))
    start_scheduler(settings)
    TRADING_STATE.set(trading_state_value(trading_controls.snapshot().state.value))
    logger.info(
        "app_startup",
        app_env=settings.app_env.value,
        trading_mode=mode.value,
        live_allowed=settings.is_live_trading_allowed(),
        alpaca_configured=bool(settings.alpaca_api_key and settings.alpaca_api_secret),
        allowlist_size=len(settings.trade_allowlist),
        enable_scheduler=settings.enable_scheduler,
        enable_broker_orders=settings.enable_broker_orders,
        broker_provider=settings.broker_provider,
        require_manual_order_approval=settings.require_manual_order_approval,
        phase=5,
    )
    yield
    await stop_scheduler()
    logger.info("app_shutdown")


app = FastAPI(
    title="Investor",
    description="Six-agent AI investment firm (paper trading first)",
    version="0.10.0",
    lifespan=lifespan,
)
app.include_router(collection_router)
app.include_router(analysis_router)
app.include_router(trading_router)
app.include_router(workflow_router)
app.include_router(portfolio_router)
app.include_router(dashboard_router)
app.include_router(market_router)
app.include_router(daily_workflow_router)
app.include_router(data_router)
app.include_router(broker_router)
app.include_router(execution_router)
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
        "version": "0.10.0",
        "trading_mode": require_execution_allowed(settings).value,
        "live_trading_allowed": settings.is_live_trading_allowed(),
        "alpaca_configured": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
        "enable_scheduler": settings.enable_scheduler,
        "enable_broker_orders": settings.enable_broker_orders,
        "enable_broker_connection": settings.enable_broker_connection,
        "broker_provider": settings.broker_provider,
        "enable_live_trading": settings.enable_live_trading,
        "require_manual_order_approval": settings.require_manual_order_approval,
        "enable_external_data": settings.enable_external_data,
        "phase": 5,
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
        "enable_scheduler": settings.enable_scheduler,
        "enable_broker_orders": settings.enable_broker_orders,
        "enable_automated_execution": settings.enable_automated_execution,
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
