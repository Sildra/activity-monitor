from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from activity_monitor.api.deps import SessionDep
from activity_monitor.model.event import SourceInfo
from activity_monitor.services.events import store_events
from activity_monitor.services.sources import PushSource, SourceProvider

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


@router.post("/ingest/event")
async def ingest_event(payload: dict[str, Any], request: Request, session: SessionDep) -> dict[str, int]:
    """Accept a single event or a batch; the payload's ``source`` names its producer."""
    body = dict(payload)
    source = body.pop("source", None)

    if not isinstance(source, str) or not source:
        raise HTTPException(400, "Payload must carry a non-empty 'source' name")

    # An unregistered name gets a transient push provider, so any client can report
    # under its own name without touching the config. Nothing is remembered:
    # `/api/sources` keeps listing only the configured sources, and a configured
    # source (e.g. `windows` or `git`) is reused so its settings apply and its
    # heartbeats still feed the user timeline's staleness check.
    provider = request.app.state.sources.get(source)
    provider = provider or PushSource(source, request.app.state.config.source_settings(source))
    events = body.get("events") if isinstance(body.get("events"), list) else [body]

    return {"accepted": await store_events(session, provider, events)}
