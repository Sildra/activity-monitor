"""Timeline computation: presence lanes/segments and the user's spans/intervals."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from activity_monitor.model.event import ActivityEvent, EventOut
from activity_monitor.model.program import Program, ProgramSegment
from activity_monitor.model.timeline import UserInterval
from activity_monitor.util.time import as_utc


def program_bounds(program: Program, query_end: datetime, now: datetime) -> tuple[datetime, datetime]:
    started = as_utc(program.started_at)
    ended = as_utc(program.ended_at) if program.ended_at is not None else min(query_end, now)

    return started, ended


def active_ranges(programs: list[Program], query_end: datetime, now: datetime) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []

    for program in programs:
        started, ended = program_bounds(program, query_end, now)

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


def compose_segments(
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


def assign_lanes(programs: list[Program], query_end: datetime, now: datetime) -> list[list[Program]]:
    lanes: list[list[Program]] = []
    lane_end: list[datetime] = []

    for program in programs:
        started, ended = program_bounds(program, query_end, now)
        idx = next((index for index, last in enumerate(lane_end) if last <= started), len(lane_end))

        if idx == len(lane_end):
            lanes.append([])
            lane_end.append(ended)
        else:
            lane_end[idx] = max(lane_end[idx], ended)

        lanes[idx].append(program)

    return lanes


def event_program_id(event: ActivityEvent) -> str | None:
    raw = (event.data or {}).get("raw") or {}
    program = raw.get("program") or {}

    return program.get("id")


async def program_events(
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

        started, ended = program_bounds(program, query_end, now)

        if started <= timestamp <= ended:
            return True

    return False


def distribute_events(
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
        program_id = event_program_id(event)

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


def clamp_span(
    start: datetime, end: datetime, query_start: datetime, query_end: datetime
) -> tuple[datetime, datetime] | None:
    start = max(start, query_start)
    end = min(end, query_end)

    if end <= start:
        return None

    return start, end


def focus_spans(events: list[ActivityEvent], open_end: datetime) -> list[tuple[datetime, datetime, ActivityEvent]]:
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


def user_intervals(
    events: list[ActivityEvent],
    start_kind: str,
    end_kind: str,
    query_start: datetime,
    query_end: datetime,
    open_end: datetime,
) -> list[UserInterval]:
    result: list[UserInterval] = []

    for interval_start, interval_end in _pair_intervals(events, start_kind, end_kind, open_end):
        clamped = clamp_span(interval_start, interval_end, query_start, query_end)

        if clamped is not None:
            result.append(UserInterval(start=clamped[0], end=clamped[1]))

    return result
