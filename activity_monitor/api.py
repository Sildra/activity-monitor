from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from activity_monitor.db import get_session
from activity_monitor.models import (
    ActivityEvent,
    ActorLane,
    AgenticTimeline,
    CodeTimeline,
    ConsolidateOut,
    EventOut,
    PresenceLane,
    PresenceTimeline,
    Program,
    ProgramOut,
    ProgramRef,
    ProgramSegment,
    ProgramStart,
    SourceInfo,
    StatsOut,
    TaskSummary,
    UserInterval,
    UserLane,
    UserSpan,
    UserTimeline,
    as_utc,
)
from activity_monitor.sources import SourceProvider

router = APIRouter(prefix="/api")

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_security = HTTPBasic(auto_error=False)


def require_auth(request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(_security)]) -> None:
    """Enforce HTTP Basic auth when the backend is configured with credentials.

    Mounted on the API router (see `create_app`), so it only runs when
    `general.basic_auth_username` is set; the static frontend and the OpenAPI
    docs stay public.
    """
    config = request.app.state.config
    username = config.basic_auth_username

    if not username:
        return

    if credentials is None or not (
        secrets.compare_digest(credentials.username, username)
        and secrets.compare_digest(credentials.password, config.basic_auth_password or "")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def _stale_after(request: Request) -> timedelta:
    return timedelta(seconds=request.app.state.config.stale_after_seconds)


def _provider(request: Request, name: str) -> SourceProvider:
    provider = request.app.state.sources.get(name)

    if provider is None:
        raise HTTPException(404, f"Unknown source '{name}'")

    return provider


_TASK_SET_KIND = "task.set"


async def _backfill_task(session: AsyncSession, program_id: str, host: str, task: str, at: datetime) -> None:
    """Retroactively tag a program's earlier untagged work after a ``task.set``.

    A ``/task`` command only pins the events that follow it (the plugin tags them as
    they happen), so work done before the task was set stays untagged. This walks the
    same program's events back from ``at`` and stamps them with ``task``, stopping at
    the previous ``task.set`` so an earlier task range is left intact.
    """
    program = func.json_extract(ActivityEvent.data, "$.raw.program.id")
    program_host = func.json_extract(ActivityEvent.data, "$.raw.program.host")

    previous = (
        await session.execute(
            select(func.max(ActivityEvent.timestamp)).where(
                ActivityEvent.kind == _TASK_SET_KIND,
                program == program_id,
                program_host == host,
                ActivityEvent.timestamp < at,
            )
        )
    ).scalar_one_or_none()

    stmt = (
        update(ActivityEvent)
        .where(
            program == program_id,
            program_host == host,
            ActivityEvent.kind != _TASK_SET_KIND,
            ActivityEvent.task.is_(None),
            ActivityEvent.timestamp < at,
        )
        .values(task=task)
    )

    if previous is not None:
        stmt = stmt.where(ActivityEvent.timestamp > as_utc(previous))

    await session.execute(stmt)


async def store_events(session: AsyncSession, provider: SourceProvider, raws: list[dict[str, Any]]) -> int:
    accepted = 0
    task_sets: list[tuple[str, str, str, datetime]] = []

    for raw in raws:
        canonical = provider.parse(raw)
        existing = (
            await session.execute(select(ActivityEvent).where(ActivityEvent.dedup_key == canonical["dedup_key"]))
        ).scalar_one_or_none()

        if existing is None:
            session.add(ActivityEvent(source=provider.name, **canonical))
            accepted += 1

            if canonical["kind"] == _TASK_SET_KIND and canonical.get("task"):
                program = (canonical.get("data") or {}).get("raw", {}).get("program") or {}

                if program.get("id"):
                    task_sets.append(
                        (
                            str(program["id"]),
                            str(program.get("host") or ""),
                            str(canonical["task"]),
                            canonical["timestamp"],
                        )
                    )

    await session.commit()

    for program_id, host, task, at in task_sets:
        await _backfill_task(session, program_id, host, task, at)

    if task_sets:
        await session.commit()

    return accepted


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


async def _expire_stale(session: AsyncSession, now: datetime, stale: timedelta) -> None:
    cutoff = now - stale
    stale_rows = list(
        (await session.execute(select(Program).where(Program.ended_at.is_(None), Program.last_seen_at < cutoff)))
        .scalars()
        .all()
    )

    for program in stale_rows:
        program.ended_at = program.last_seen_at

    if stale_rows:
        await session.commit()


async def _find_active_program(session: AsyncSession, program_id: str) -> Program | None:
    return (
        (
            await session.execute(
                select(Program)
                .where(Program.program_id == program_id, Program.ended_at.is_(None))
                .order_by(Program.started_at.desc())
            )
        )
        .scalars()
        .first()
    )


@router.post("/programs/start", response_model=ProgramOut)
async def program_start(payload: ProgramStart, session: SessionDep, request: Request) -> ProgramOut:
    now = datetime.now(UTC)
    await _expire_stale(session, now, _stale_after(request))

    if await _find_active_program(session, payload.program_id) is not None:
        raise HTTPException(409, f"Program '{payload.program_id}' is already active")

    program = Program(
        program_id=payload.program_id,
        source=payload.source,
        host=payload.host,
        actor=payload.actor or "opencode",
        started_at=now,
        last_seen_at=now,
    )
    session.add(program)
    await session.commit()

    return ProgramOut(program_id=program.program_id, source=program.source, host=program.host)


@router.post("/programs/beat", response_model=ProgramOut)
async def program_beat(payload: ProgramRef, session: SessionDep) -> ProgramOut:
    program = await _find_active_program(session, payload.program_id)

    if program is None:
        raise HTTPException(404, f"No active program '{payload.program_id}'")

    program.last_seen_at = datetime.now(UTC)
    await session.commit()

    return ProgramOut(program_id=program.program_id, source=program.source, host=program.host)


@router.post("/programs/end", response_model=ProgramOut)
async def program_end(payload: ProgramRef, session: SessionDep) -> ProgramOut:
    program = await _find_active_program(session, payload.program_id)

    if program is None:
        raise HTTPException(404, f"No active program '{payload.program_id}'")

    # Close at the last activity rather than now: an exit request can arrive late
    # (e.g. after the machine slept), and the silent tail is not attention.
    program.ended_at = program.last_seen_at
    await session.commit()

    return ProgramOut(program_id=program.program_id, source=program.source, host=program.host)


@router.post("/programs/consolidate", response_model=ConsolidateOut)
async def consolidate_programs(
    request: Request, session: SessionDep, idle_seconds: int = Query(3600, gt=0)
) -> ConsolidateOut:
    """Tidy the stored program history.

    Finished programs are rewritten from their own events: a program with no events is
    deleted, each one is trimmed to its first and last non-idle boundary, and an idle
    gap longer than ``idle_seconds`` splits it into separate blocks so the dead time is
    not counted as presence. Running programs (``ended_at`` unset) are left alone.
    """
    now = datetime.now(UTC)
    await _expire_stale(session, now, _stale_after(request))

    programs = list(
        (await session.execute(select(Program).where(Program.ended_at.is_not(None)).order_by(Program.started_at.asc())))
        .scalars()
        .all()
    )

    threshold = timedelta(seconds=idle_seconds)
    events = await _program_events(session, {program.program_id for program in programs})
    by_pid: dict[str, list[ActivityEvent]] = {}

    for event in events:
        program_id = _event_program_id(event)

        if program_id is not None:
            by_pid.setdefault(program_id, []).append(event)

    removed = trimmed = split = 0

    for program in programs:
        program_events = by_pid.get(program.program_id, [])

        if not program_events:
            await session.delete(program)
            removed += 1
            continue

        started, ended = _program_bounds(program, now, now)
        in_range = [event for event in program_events if started <= as_utc(event.timestamp) <= ended]

        if not in_range:
            continue

        segments = _compose_segments(
            [(started, ended)], [EventOut.from_event(event) for event in in_range], started, ended
        )
        active = [(segment.start, segment.end) for segment in segments if segment.state != "idle"]

        if not active:
            continue

        groups: list[tuple[datetime, datetime]] = []

        for seg_start, seg_end in active:
            if groups and seg_start - groups[-1][1] <= threshold:
                groups[-1] = (groups[-1][0], seg_end)
            else:
                groups.append((seg_start, seg_end))

        if groups[0] != (started, ended):
            trimmed += 1

        program.started_at = groups[0][0]
        program.ended_at = groups[0][1]
        program.last_seen_at = groups[0][1]

        for group_start, group_end in groups[1:]:
            session.add(
                Program(
                    program_id=program.program_id,
                    source=program.source,
                    host=program.host,
                    actor=program.actor,
                    started_at=group_start,
                    last_seen_at=group_end,
                    ended_at=group_end,
                )
            )
            split += 1

    await session.commit()

    return ConsolidateOut(removed=removed, trimmed=trimmed, split=split)


def _program_bounds(program: Program, query_end: datetime, now: datetime) -> tuple[datetime, datetime]:
    started = as_utc(program.started_at)
    ended = as_utc(program.ended_at) if program.ended_at is not None else min(query_end, now)

    return started, ended


def _active_ranges(programs: list[Program], query_end: datetime, now: datetime) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []

    for program in programs:
        started, ended = _program_bounds(program, query_end, now)

        if ended > started:
            ranges.append((started, ended))

    ranges.sort(key=lambda item: item[0])

    merged: list[tuple[datetime, datetime]] = []

    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    return merged


_EVENT_STATE: dict[str, str] = {
    "prompt.begin": "active",
    "prompt.end": "idle",
    "permission.asked": "waiting",
    "permission.replied": "active",
    "question.asked": "waiting",
    "question.replied": "active",
}


def _compose_segments(
    ranges: list[tuple[datetime, datetime]], events: list[EventOut], query_start: datetime, query_end: datetime
) -> list[ProgramSegment]:
    switches = sorted(
        (event.timestamp, state) for event in events if (state := _EVENT_STATE.get(event.kind)) is not None
    )

    segments: list[ProgramSegment] = []
    cursor = query_start

    for r_start, r_end in ranges:
        r_start = max(r_start, query_start)
        r_end = min(r_end, query_end)

        if r_end <= r_start:
            continue

        if cursor < r_start:
            segments.append(ProgramSegment(start=cursor, end=r_start, state="closed"))

        state = "idle"
        pos = r_start

        for ts, next_state in switches:
            if ts >= r_end:
                break

            if ts <= r_start:
                state = next_state
                continue

            if next_state == state:
                continue

            if ts > pos:
                segments.append(ProgramSegment(start=pos, end=ts, state=state))
                pos = ts

            state = next_state

        if pos < r_end:
            segments.append(ProgramSegment(start=pos, end=r_end, state=state))

        cursor = max(cursor, r_end)

    if cursor < query_end:
        segments.append(ProgramSegment(start=cursor, end=query_end, state="closed"))

    return segments


def _assign_lanes(programs: list[Program], query_end: datetime, now: datetime) -> list[list[Program]]:
    lanes: list[list[Program]] = []
    lane_end: list[datetime] = []

    for program in programs:
        started, ended = _program_bounds(program, query_end, now)
        idx = next((index for index, last in enumerate(lane_end) if last <= started), len(lane_end))

        if idx == len(lane_end):
            lanes.append([])
            lane_end.append(ended)
        else:
            lane_end[idx] = max(lane_end[idx], ended)

        lanes[idx].append(program)

    return lanes


def _event_program_id(event: ActivityEvent) -> str | None:
    raw = (event.data or {}).get("raw") or {}
    program = raw.get("program") or {}

    return program.get("id")


async def _program_events(
    session: AsyncSession, program_ids: set[str], query_start: datetime | None = None, query_end: datetime | None = None
) -> list[ActivityEvent]:
    if not program_ids:
        return []

    stmt = (
        select(ActivityEvent)
        .where(func.json_extract(ActivityEvent.data, "$.raw.program.id").in_(program_ids))
        .order_by(ActivityEvent.timestamp.asc(), ActivityEvent.id.asc())
    )

    if query_start is not None:
        stmt = stmt.where(ActivityEvent.timestamp >= query_start)

    if query_end is not None:
        stmt = stmt.where(ActivityEvent.timestamp <= query_end)

    return list((await session.execute(stmt)).scalars().all())


def _lane_covers(
    programs: list[Program], program_id: str, timestamp: datetime, query_end: datetime, now: datetime
) -> bool:
    for program in programs:
        if program.program_id != program_id:
            continue

        started, ended = _program_bounds(program, query_end, now)

        if started <= timestamp <= ended:
            return True

    return False


def _distribute_events(
    lanes: list[list[Program]], events: list[ActivityEvent], query_end: datetime, now: datetime
) -> list[list[ActivityEvent]]:
    """Attach each program event to exactly one lane.

    Events are matched to programs by ``data.raw.program.id``. Consolidation can split
    one program across several non-overlapping blocks that share the id, which may land
    in different lanes when another program fills the gap; in that case each event goes
    to the lane whose block covers its timestamp so a dot is never drawn twice.
    """
    owners: dict[str, list[int]] = {}

    for index, programs in enumerate(lanes):
        for program in programs:
            owners.setdefault(program.program_id, []).append(index)

    buckets: list[list[ActivityEvent]] = [[] for _ in lanes]

    for event in events:
        program_id = _event_program_id(event)

        if program_id is None:
            continue

        candidates = owners.get(program_id)

        if not candidates:
            continue

        if len(candidates) == 1:
            buckets[candidates[0]].append(event)
            continue

        target = next(
            (
                index
                for index in candidates
                if _lane_covers(lanes[index], program_id, as_utc(event.timestamp), query_end, now)
            ),
            candidates[0],
        )
        buckets[target].append(event)

    return buckets


@router.get("/timelines/presence", response_model=PresenceTimeline)
async def presence_timeline(
    request: Request, session: SessionDep, start: datetime | None = Query(None), end: datetime | None = Query(None)
) -> PresenceTimeline:
    now = datetime.now(UTC)
    query_end = end or now
    query_start = start or (now - timedelta(hours=24))

    if query_start >= query_end:
        raise HTTPException(400, "'start' must be before 'end'")

    await _expire_stale(session, now, _stale_after(request))

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
        for parallel_id, lane_programs in enumerate(_assign_lanes(key_programs, query_end, now)):
            lane_specs.append((source, host, parallel_id, lane_programs))

    program_ids = {program.program_id for _, _, _, lane_programs in lane_specs for program in lane_programs}
    events = await _program_events(session, program_ids, query_start, query_end)
    buckets = _distribute_events([lane_programs for _, _, _, lane_programs in lane_specs], events, query_end, now)

    lanes: list[PresenceLane] = []

    for index, (source, host, parallel_id, lane_programs) in enumerate(lane_specs):
        ranges = _active_ranges(lane_programs, query_end, now)
        lane_events = [EventOut.from_event(event) for event in buckets[index]]
        segments = _compose_segments(ranges, lane_events, query_start, query_end)

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


_USER_SOURCE = "windows"
_HEARTBEAT_KIND = "heartbeat"
_USER_KINDS = ("focus", "idle.start", "idle.end", "lock", "unlock", _HEARTBEAT_KIND)


def _clamp_span(
    start: datetime, end: datetime, query_start: datetime, query_end: datetime
) -> tuple[datetime, datetime] | None:
    start = max(start, query_start)
    end = min(end, query_end)

    if end <= start:
        return None

    return start, end


def _focus_spans(events: list[ActivityEvent], open_end: datetime) -> list[tuple[datetime, datetime, ActivityEvent]]:
    """Turn ``focus`` events into spans.

    A close carries ``duration_ms`` and ends at its ``timestamp``. A trailing
    open span (the last ``focus`` has no ``duration_ms``) is only extended to
    ``open_end`` — the last observed event plus the staleness window — so a
    watcher that stopped reporting does not leave a multi-day "active" bar.
    """
    focus = [event for event in events if event.kind == "focus"]
    spans: list[tuple[datetime, datetime, ActivityEvent]] = []

    for event in focus:
        if event.duration_ms is None:
            continue

        end = as_utc(event.timestamp)
        spans.append((end - timedelta(milliseconds=event.duration_ms), end, event))

    if focus and focus[-1].duration_ms is None:
        start = as_utc(focus[-1].timestamp)

        if open_end > start:
            spans.append((start, open_end, focus[-1]))

    return spans


def _pair_intervals(
    events: list[ActivityEvent], start_kind: str, end_kind: str, open_end: datetime
) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    open_start: datetime | None = None

    for event in events:
        if event.kind == start_kind:
            open_start = as_utc(event.timestamp)
        elif event.kind == end_kind:
            end = as_utc(event.timestamp)
            start = open_start

            if start is None and event.duration_ms is not None:
                start = end - timedelta(milliseconds=event.duration_ms)

            if start is not None and end > start:
                intervals.append((start, end))

            open_start = None

    if open_start is not None and open_end > open_start:
        intervals.append((open_start, open_end))

    return intervals


def _user_intervals(
    events: list[ActivityEvent],
    start_kind: str,
    end_kind: str,
    query_start: datetime,
    query_end: datetime,
    open_end: datetime,
) -> list[UserInterval]:
    result: list[UserInterval] = []

    for interval_start, interval_end in _pair_intervals(events, start_kind, end_kind, open_end):
        clamped = _clamp_span(interval_start, interval_end, query_start, query_end)

        if clamped is not None:
            result.append(UserInterval(start=clamped[0], end=clamped[1]))

    return result


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
                    ActivityEvent.source == _USER_SOURCE,
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

    stale = _stale_after(request)
    last_seen = as_utc(events[-1].timestamp) if events else now
    # A span that is still open (no closing event) is only trusted while the
    # watcher keeps reporting; clip it near the last event we actually saw so a
    # stopped watcher does not leave a multi-day "active"/"idle"/"locked" bar.
    open_end = min(query_end, now, last_seen + stale)

    by_process: dict[str, list[UserSpan]] = {}
    totals: dict[str, int] = {}

    for span_start, span_end, event in _focus_spans(events, open_end):
        clamped = _clamp_span(span_start, span_end, query_start, query_end)

        if clamped is None:
            continue

        left, right = clamped
        process = event.task or "unknown"
        by_process.setdefault(process, []).append(UserSpan(start=left, end=right, title=event.title))
        totals[process] = totals.get(process, 0) + int((right - left).total_seconds() * 1000)

    lanes = [
        UserLane(process=process, label=process, spans=sorted(spans, key=lambda span: span.start))
        for process, spans in sorted(by_process.items(), key=lambda item: totals[item[0]], reverse=True)
    ]

    return UserTimeline(
        start=query_start,
        end=query_end,
        lanes=lanes,
        idle=_user_intervals(events, "idle.start", "idle.end", query_start, query_end, open_end),
        locked=_user_intervals(events, "lock", "unlock", query_start, query_end, open_end),
    )


_CODE_SOURCE = "git"
_CODE_KINDS = ("commit", "push")


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


def _apply_filters(stmt, q: str | None, source: str | None, actor: str | None, kind: str | None, task: str | None):
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            ActivityEvent.title.ilike(pattern) | ActivityEvent.detail.ilike(pattern) | ActivityEvent.task.ilike(pattern)
        )

    if source:
        stmt = stmt.where(ActivityEvent.source == source)

    if actor:
        stmt = stmt.where(ActivityEvent.actor == actor)

    if kind:
        stmt = stmt.where(ActivityEvent.kind == kind)

    if task:
        stmt = stmt.where(ActivityEvent.task == task)

    return stmt


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


def _merge_focus_spans(events: list[ActivityEvent]) -> list[EventOut]:
    """Collapse each windows focus open/close pair into a single span event.

    The desktop watcher records a span as an open (no ``duration_ms``) followed by a
    close (with ``duration_ms``). Read back verbatim, every span shows up twice; here
    they become one event dated at the span start and carrying the span's duration.
    """
    ordered = sorted(events, key=lambda event: (event.timestamp, event.id or 0))
    merged: list[EventOut] = []
    open_event: ActivityEvent | None = None

    for event in ordered:
        if event.source != _USER_SOURCE or event.kind != "focus":
            merged.append(EventOut.from_event(event))
            continue

        if event.duration_ms is None:
            open_event = event
            continue

        if open_event is not None:
            span = EventOut.from_event(open_event)
            span.duration_ms = event.duration_ms
            merged.append(span)
            open_event = None
        else:
            merged.append(EventOut.from_event(event))

    if open_event is not None:
        merged.append(EventOut.from_event(open_event))

    merged.sort(key=lambda event: (event.timestamp, event.id or 0), reverse=True)
    return merged


@router.get("/tasks", response_model=list[EventOut])
async def list_tasks(
    session: SessionDep,
    q: str | None = Query(None),
    source: str | None = Query(None),
    actor: str | None = Query(None),
    kind: str | None = Query(None),
    task: str | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    limit: int = Query(500, le=5000),
) -> list[EventOut]:
    stmt = select(ActivityEvent).order_by(ActivityEvent.timestamp.desc())
    stmt = _apply_filters(stmt, q, source, actor, kind, task)

    if kind is None:
        # Liveness pings are not user-meaningful; only show them when asked for.
        stmt = stmt.where(ActivityEvent.kind != _HEARTBEAT_KIND)

    if start is not None:
        stmt = stmt.where(ActivityEvent.timestamp >= start)

    if end is not None:
        stmt = stmt.where(ActivityEvent.timestamp <= end)

    events = list((await session.execute(stmt.limit(limit))).scalars().all())

    return _merge_focus_spans(events)


@router.get("/tasks/summary", response_model=list[TaskSummary])
async def task_summary(
    session: SessionDep,
    q: str | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
) -> list[TaskSummary]:
    stmt = select(
        ActivityEvent.task, func.count(), func.min(ActivityEvent.timestamp), func.max(ActivityEvent.timestamp)
    ).where(ActivityEvent.task.is_not(None))

    if q:
        stmt = stmt.where(ActivityEvent.task.ilike(f"%{q}%"))

    if start is not None:
        stmt = stmt.where(ActivityEvent.timestamp >= start)

    if end is not None:
        stmt = stmt.where(ActivityEvent.timestamp <= end)

    stmt = stmt.group_by(ActivityEvent.task).order_by(func.count().desc())

    rows = (await session.execute(stmt)).all()

    return [
        TaskSummary(task=task, count=count, first=as_utc(first), last=as_utc(last))
        for task, count, first, last in rows
        if task
    ]


@router.get("/actors", response_model=list[str])
async def list_actors(session: SessionDep) -> list[str]:
    rows = (await session.execute(select(distinct(ActivityEvent.actor)).order_by(ActivityEvent.actor))).scalars().all()

    return list(rows)


@router.get("/kinds", response_model=list[str])
async def list_kinds(session: SessionDep) -> list[str]:
    rows = (await session.execute(select(distinct(ActivityEvent.kind)).order_by(ActivityEvent.kind))).scalars().all()

    return list(rows)


@router.get("/stats", response_model=StatsOut)
async def stats(session: SessionDep) -> StatsOut:
    total = (
        await session.execute(
            select(func.count()).select_from(ActivityEvent).where(ActivityEvent.kind != _HEARTBEAT_KIND)
        )
    ).scalar_one()
    sources = (await session.execute(select(func.count(distinct(ActivityEvent.source))))).scalar_one()
    actors = (await session.execute(select(func.count(distinct(ActivityEvent.actor))))).scalar_one()
    tasks = (await session.execute(select(func.count(distinct(ActivityEvent.task))))).scalar_one()

    return StatsOut(events=total, sources=sources, actors=actors, tasks=tasks)
