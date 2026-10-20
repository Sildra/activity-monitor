"""Source listing and the generic ingest endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from activity_monitor.api.deps import SessionDep
from activity_monitor.model.event import ActivityEvent
from activity_monitor.services.events import store_events
from activity_monitor.services.sources import PushSource

router = APIRouter(prefix="/api")


@router.get("/sources", response_model=list[str])
async def list_sources(session: SessionDep) -> list[str]:
    """Names of every source seen in the events table (alphabetical).

    Sources are config-free producers that post to the ingest endpoint, so the
    listing is what actually showed up rather than a configured set.
    """
    rows = await session.execute(select(ActivityEvent.source).distinct().order_by(ActivityEvent.source))

    return list(rows.scalars())


@router.post("/ingest/event")
async def ingest_event(payload: dict[str, Any], request: Request, session: SessionDep) -> dict[str, int]:
    """Accept a single event or a batch; the payload's ``source`` names its producer."""
    body = dict(payload)
    source = body.pop("source", None)

    if not isinstance(source, str) or not source:
        raise HTTPException(400, "Payload must carry a non-empty 'source' name")

    # An unregistered name gets a transient push provider, so any client can report
    # under its own name without any configuration. The built-in sources (`windows`,
    # `git`) are always registered, so their heartbeats and parsing still apply.
    provider = request.app.state.sources.get(source) or PushSource(source, {})
    events = body.get("events") if isinstance(body.get("events"), list) else [body]

    return {"accepted": await store_events(session, provider, events)}
