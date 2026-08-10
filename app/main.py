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
from app.api.intraday import router as intraday_router
from app.api.market import router as market_router
from app.api.operations_phase7 import router as operations_phase7_router
from app.api.performance import router as performance_router
from app.api.portfolio import router as portfolio_router
from app.api.trading import router as trading_router
from app.api.universe import router as universe_router
from app.api.workflow import router as workflow_router
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import get_logger, setup_logging
from app.core.metrics import TRADING_STATE, trading_state_value
from app.core.scheduler import start_scheduler, stop_scheduler, upcoming_jobs
from app.core.security import require_execution_allowed
from app.execution.safety_controls import trading_controls
from app.services.llm_budget import snapshot_llm_budget
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
            logger.info(
                "startup_recovery",
                **{k: recovery[k] for k in ("reclaimed_leases", "emergency_stop")},
            )
            if settings.enable_broker_connection or settings.enable_broker_orders:
                from app.intraday.recovery import IntradayRecoveryService

                intra = await IntradayRecoveryService(session, settings=settings).run()
                logger.info(
                    "startup_intraday_recovery",
                    recovery_id=intra.get("recovery_id"),
                    emergency_stop=intra.get("emergency_stop"),
                    new_orders_allowed=intra.get("new_orders_allowed"),
                    actions=intra.get("actions"),
                )
            await session.commit()
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
        phase=7,
    )
    yield
    await stop_scheduler()
    try:
        from app.brokers.factory import disconnect_broker

        await disconnect_broker()
    except Exception as exc:  # noqa: BLE001
        logger.warning("broker_disconnect_on_shutdown_failed", error=str(exc)[:160])
    logger.info("app_shutdown")


app = FastAPI(
    title="Investor",
    description="Six-agent AI investment firm (paper trading first)",
    version="0.12.0",
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
app.include_router(intraday_router)
app.include_router(performance_router)
app.include_router(operations_phase7_router)
app.include_router(universe_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/favicon.ico")
async def favicon_ico() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico")


@app.get("/favicon.svg")
async def favicon_svg() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/dashboard")
async def dashboard_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": "0.12.0",
        "env": settings.app_env.value,
        "trading_mode": require_execution_allowed(settings).value,
        "live_trading_allowed": settings.is_live_trading_allowed(),
        "alpaca_configured": bool(settings.alpaca_api_key and settings.alpaca_api_secret),
        "enable_scheduler": settings.enable_scheduler,
        "enable_broker_orders": settings.enable_broker_orders,
        "enable_broker_connection": settings.enable_broker_connection,
        "broker_provider": settings.broker_provider,
        "enable_live_trading": settings.enable_live_trading,
        "require_manual_order_approval": settings.require_manual_order_approval,
        "intraday_operation_mode": settings.intraday_operation_mode,
        "enable_external_data": settings.enable_external_data,
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
        "llm_budget": snapshot_llm_budget(settings).to_dict(),
        "note": "Phase 7 dashboard and Prometheus metrics online",
        "endpoints": {
            "dashboard": "GET /dashboard",
            "metrics": "GET /metrics",
            "summary": "GET /dashboard/summary",
            "briefing": "GET /dashboard/briefing",
            "decisions": "GET /decisions",
            "agent_runs": "GET /agents/runs",
            "events": "GET /events",
            "premarket_run": "POST /workflow/premarket/run",
            "portfolio": "GET /portfolio",
            "emergency_stop": "POST /trading/emergency-stop",
            "restart_trading": "POST /trading/restart",
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
