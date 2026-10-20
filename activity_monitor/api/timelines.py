from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from activity_monitor.api.deps import SessionDep, stale_after
from activity_monitor.model.event import ActivityEvent, EventOut
from activity_monitor.model.program import PresenceLane, PresenceTimeline, Program
from activity_monitor.model.timeline import ActorLane, AgenticTimeline, CodeTimeline, UserLane, UserSpan, UserTimeline
from activity_monitor.services.events import HEARTBEAT_KIND
from activity_monitor.services.programs import expire_stale
from activity_monitor.services.sources import UNKNOWN_TASK, USER_SOURCE
from activity_monitor.services.timelines import (
    active_ranges,
    assign_lanes,
    clamp_span,
    compose_segments,
    distribute_events,
    focus_spans,
    program_events,
    user_intervals,
)
from activity_monitor.util.time import as_utc

router = APIRouter(prefix="/api")


@router.get("/timelines/presence", response_model=PresenceTimeline)
async def presence_timeline(
    request: Request, session: SessionDep, start: datetime | None = Query(None), end: datetime | None = Query(None)
) -> PresenceTimeline:
    now = datetime.now(UTC)
    query_end = end or now
    query_start = start or (now - timedelta(hours=24))

    if query_start >= query_end:
        raise HTTPException(400, "'start' must be before 'end'")

    await expire_stale(session, now, stale_after(request))

    programs = list(
        (
            await session.execute(
                select(Program)
                .where(Program.started_at <= query_end, Program.ended_at.is_(None) | (Program.ended_at >= query_start))
                .order_by(Program.started_at.asc())
            )
        )
        .scalars()
        .all()
    )

    by_key: dict[tuple[str, str], list[Program]] = {}

    for program in programs:
        by_key.setdefault((program.source, program.host), []).append(program)

    lane_specs: list[tuple[str, str, int, list[Program]]] = []

    for (source, host), key_programs in by_key.items():
        for parallel_id, lane_programs in enumerate(assign_lanes(key_programs, query_end, now)):
            lane_specs.append((source, host, parallel_id, lane_programs))

    program_ids = {program.program_id for _, _, _, lane_programs in lane_specs for program in lane_programs}
    events = await program_events(session, program_ids, query_start, query_end)
    buckets = distribute_events([lane_programs for _, _, _, lane_programs in lane_specs], events, query_end, now)

    lanes: list[PresenceLane] = []

    for index, (source, host, parallel_id, lane_programs) in enumerate(lane_specs):
        ranges = active_ranges(lane_programs, query_end, now)
        lane_events = [EventOut.from_event(event) for event in buckets[index]]
        segments = compose_segments(ranges, lane_events, query_start, query_end)

        lanes.append(
            PresenceLane(
                key=f"{source}-{host}-{parallel_id}",
                source=source,
                host=host,
                parallel_id=parallel_id,
                segments=segments,
                events=lane_events,
            )
        )

    lanes.sort(key=lambda lane: lane.key)

    return PresenceTimeline(start=query_start, end=query_end, lanes=lanes)


# `HEARTBEAT_KIND` is only listed for legacy rows written before pings stopped
# being stored; new pings never reach the events table.
_USER_KINDS = ("focus", "idle.start", "idle.end", "lock", "unlock", HEARTBEAT_KIND)


@router.get("/timelines/user", response_model=UserTimeline)
async def user_timeline(
    request: Request, session: SessionDep, start: datetime | None = Query(None), end: datetime | None = Query(None)
) -> UserTimeline:
    now = datetime.now(UTC)
    query_end = end or now
    query_start = start or (now - timedelta(hours=24))

    if query_start >= query_end:
        raise HTTPException(400, "'start' must be before 'end'")

    events = list(
        (
            await session.execute(
                select(ActivityEvent)
                .where(
                    ActivityEvent.source == USER_SOURCE,
                    ActivityEvent.timestamp >= query_start,
                    ActivityEvent.timestamp <= query_end,
                    ActivityEvent.kind.in_(_USER_KINDS),
                )
                .order_by(ActivityEvent.timestamp.asc(), ActivityEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )

    stale = stale_after(request)
    # A span that is still open (no closing event) is only trusted while the
    # watcher keeps reporting; clip it near the last thing we heard from it so a
    # stopped watcher does not leave a multi-day "active"/"idle"/"locked" bar.
    # Heartbeats are not stored, so the latest in-memory ping stands in for the
    # events a running watcher would otherwise keep emitting.
    provider = request.app.state.sources.get(USER_SOURCE)
    seen = [as_utc(events[-1].timestamp)] if events else []
    last_ping = provider.last_heartbeat if provider is not None else None

    if last_ping is not None:
        seen.append(last_ping)

    last_seen = max(seen) if seen else now
    open_end = min(query_end, now, last_seen + stale)

    by_task: dict[str, list[UserSpan]] = {}
    totals: dict[str, int] = {}

    for span_start, span_end, event in focus_spans(events, open_end):
        clamped = clamp_span(span_start, span_end, query_start, query_end)

        if clamped is None:
            continue

        left, right = clamped
        # Lanes group by task: focus events report `ACTIVITY_MONITOR_TASK` or the
        # default `Unknown` — a task is a unit of work, never a process.
        task = event.task or UNKNOWN_TASK
        by_task.setdefault(task, []).append(UserSpan(start=left, end=right, title=event.title))
        totals[task] = totals.get(task, 0) + int((right - left).total_seconds() * 1000)

    lanes = [
        UserLane(task=task, label=task, spans=sorted(spans, key=lambda span: span.start))
        for task, spans in sorted(by_task.items(), key=lambda item: totals[item[0]], reverse=True)
    ]

    return UserTimeline(
        start=query_start,
        end=query_end,
        lanes=lanes,
        idle=user_intervals(events, "idle.start", "idle.end", query_start, query_end, open_end),
        locked=user_intervals(events, "lock", "unlock", query_start, query_end, open_end),
    )


_CODE_SOURCE = "git"
_CODE_KINDS = ("commit", "push", "rebase", "amend")


@router.get("/timelines/code", response_model=CodeTimeline)
async def code_timeline(
    session: SessionDep, start: datetime | None = Query(None), end: datetime | None = Query(None)
) -> CodeTimeline:
    now = datetime.now(UTC)
    query_end = end or now
    query_start = start or (now - timedelta(hours=24))

    if query_start >= query_end:
        raise HTTPException(400, "'start' must be before 'end'")

    events = list(
        (
            await session.execute(
                select(ActivityEvent)
                .where(
                    ActivityEvent.source == _CODE_SOURCE,
                    ActivityEvent.kind.in_(_CODE_KINDS),
                    ActivityEvent.timestamp >= query_start,
                    ActivityEvent.timestamp <= query_end,
                )
                .order_by(ActivityEvent.timestamp.asc(), ActivityEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )

    return CodeTimeline(start=query_start, end=query_end, events=[EventOut.from_event(event) for event in events])


@router.get("/timelines/agentic", response_model=AgenticTimeline)
async def agentic_timeline(
    session: SessionDep,
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    actor: list[str] | None = Query(None),
) -> AgenticTimeline:
    stmt = select(ActivityEvent).order_by(ActivityEvent.timestamp.asc())

    if start is not None:
        stmt = stmt.where(ActivityEvent.timestamp >= start)

    if end is not None:
        stmt = stmt.where(ActivityEvent.timestamp <= end)

    if actor:
        stmt = stmt.where(ActivityEvent.actor.in_(actor))

    events = list((await session.execute(stmt)).scalars().all())

    lanes: dict[str, list[EventOut]] = {}

    for event in events:
        lanes.setdefault(event.actor, []).append(EventOut.from_event(event))

    return AgenticTimeline(
        start=start, end=end, lanes=[ActorLane(actor=name, events=items) for name, items in sorted(lanes.items())]
    )
