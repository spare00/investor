"""Data layer API (Phase 4)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.ingestion.pipeline import DataCollectionPipeline
from app.providers.base import get_breaker, reset_breakers
from app.providers.registry import list_providers, resolve_market_provider, resolve_news_provider, resolve_sec_provider
from app.workflow.lease import LeaseError, LeaseService

router = APIRouter(prefix="/data", tags=["data"])

_LAST_RUNS: dict[str, dict[str, Any]] = {}


@router.get("/providers")
async def providers() -> dict[str, Any]:
    return {"providers": list_providers(get_settings()), "enable_external_data": get_settings().enable_external_data}


@router.get("/providers/health")
async def providers_health() -> dict[str, Any]:
    settings = get_settings()
    names = ["fixture", "alpaca", "sec_edgar"]
    health = []
    for name in names:
        br = get_breaker(name, settings)
        health.append(
            {
                "provider_name": name,
                "healthy": br.allow(),
                "failures": br.failures,
                "circuit_open": br.opened_at is not None and not br.allow(),
            }
        )
    return {"health": health}


@router.get("/market/quotes")
async def market_quotes(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"quotes": [] if not run else run.get("quotes", []), "note": "from_last_collection_cache"}


@router.get("/market/bars")
async def market_bars() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"bars": [] if not run else run.get("bars", [])}


@router.get("/market/premarket")
async def market_premarket() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"premarket": [] if not run else run.get("premarket", [])}


@router.get("/news")
async def news() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"news": [] if not run else run.get("news", [])}


@router.get("/news/events")
async def news_events() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"clusters": [] if not run else run.get("news_clusters", [])}


@router.get("/sec/filings")
async def sec_filings() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"filings": [] if not run else run.get("filings", [])}


@router.get("/macro/events")
async def macro_events() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"events": [] if not run else run.get("economic_events", [])}


@router.get("/quality")
async def quality() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"quality_summary": None if not run else run.get("quality_summary")}


@router.get("/conflicts")
async def conflicts() -> dict[str, Any]:
    run = _LAST_RUNS.get("latest")
    return {"conflicts": [] if not run else run.get("conflicts", [])}


@router.get("/collection-runs")
async def collection_runs() -> dict[str, Any]:
    return {"runs": list(_LAST_RUNS.values())[-20:]}


@router.get("/collection-runs/{collection_run_id}")
async def collection_run(collection_run_id: str) -> dict[str, Any]:
    for run in _LAST_RUNS.values():
        if run.get("collection_run_id") == collection_run_id:
            return {"run": run}
    raise HTTPException(status_code=404, detail="not_found")


async def _collect(
    session: AsyncSession,
    collection_type: str,
    *,
    symbols: list[str] | None,
    fixture: bool,
    idempotency_key: str | None,
) -> dict[str, Any]:
    settings = get_settings()
    leases = LeaseService(session, settings)
    key = f"collect:{collection_type}:{idempotency_key or 'default'}"
    try:
        await leases.acquire(key, "data-api")
    except LeaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        result = await DataCollectionPipeline(
            session, settings=settings, fixture_mode=fixture or not settings.enable_external_data
        ).collect(collection_type, symbols=symbols)
        payload = result.to_dict()
        payload["quotes"] = [q.model_dump(mode="json") for q in result.quotes]
        payload["bars"] = [b.model_dump(mode="json") for b in result.bars]
        payload["premarket"] = [p.model_dump(mode="json") for p in result.premarket]
        payload["news"] = [n.model_dump(mode="json") for n in result.news]
        payload["news_clusters"] = [c.model_dump(mode="json") for c in result.news_clusters]
        payload["filings"] = [f.model_dump(mode="json") for f in result.filings]
        payload["economic_events"] = [e.model_dump(mode="json") for e in result.economic_events]
        payload["conflicts"] = [c.model_dump(mode="json") for c in result.conflicts]
        payload["contexts"] = result.contexts
        _LAST_RUNS["latest"] = payload
        _LAST_RUNS[str(result.collection_run_id)] = payload
        await session.commit()
        return payload
    finally:
        try:
            await leases.release(key, "data-api")
        except LeaseError:
            pass


@router.post("/collect/premarket")
async def collect_premarket(
    fixture: bool = True,
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return await _collect(session, "PREMARKET", symbols=None, fixture=fixture, idempotency_key=idempotency_key)


@router.post("/collect/revalidation")
async def collect_revalidation(
    fixture: bool = True,
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return await _collect(
        session, "PREOPEN_REVALIDATION", symbols=None, fixture=fixture, idempotency_key=idempotency_key
    )


@router.post("/collect/intraday")
async def collect_intraday(
    symbols: str | None = None,
    fixture: bool = True,
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    syms = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    return await _collect(session, "INTRADAY", symbols=syms, fixture=fixture, idempotency_key=idempotency_key)


@router.post("/collect/postmarket")
async def collect_postmarket(
    fixture: bool = True,
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return await _collect(session, "POSTMARKET", symbols=None, fixture=fixture, idempotency_key=idempotency_key)


@router.post("/collect/on-demand")
async def collect_on_demand(
    fixture: bool = True,
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    return await _collect(session, "ON_DEMAND", symbols=None, fixture=fixture, idempotency_key=idempotency_key)


@router.post("/events/rebuild")
async def events_rebuild(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await DataCollectionPipeline(session, fixture_mode=True).collect("ON_DEMAND")
    return {"market_events": result.market_events, "broker_orders": False}
