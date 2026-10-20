from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from activity_monitor.api.deps import SessionDep
from activity_monitor.model.event import SourceInfo
from activity_monitor.services.events import store_events
from activity_monitor.services.sources import SourceProvider

router = APIRouter(prefix="/api")


def _provider(request: Request, name: str) -> SourceProvider:
    provider = request.app.state.sources.get(name)

    if provider is None:
        raise HTTPException(404, f"Unknown source '{name}'")

    return provider


@router.get("/sources", response_model=list[SourceInfo])
async def list_sources(request: Request) -> list[SourceInfo]:
    return [
        SourceInfo(name=provider.name, kind=provider.KIND, pull=provider.is_pull)
        for provider in request.app.state.sources.values()
    ]


@router.post("/sources/{name}/fetch")
async def fetch_source(name: str, request: Request, session: SessionDep) -> dict[str, int]:
    provider = _provider(request, name)

    if not provider.is_pull:
        raise HTTPException(400, f"Source '{name}' is push-based and cannot be fetched")

    raws = await asyncio.to_thread(provider.fetch)

    return {"fetched": len(raws), "accepted": await store_events(session, provider, raws)}


@router.post("/ingest/{name}")
async def ingest(name: str, payload: dict[str, Any], request: Request, session: SessionDep) -> dict[str, int]:
    provider = _provider(request, name)

    events = payload.get("events") if isinstance(payload.get("events"), list) else [payload]

    return {"accepted": await store_events(session, provider, events)}
