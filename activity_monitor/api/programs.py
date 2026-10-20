from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from activity_monitor.api.deps import SessionDep, stale_after
from activity_monitor.model.event import ActivityEvent, EventOut
from activity_monitor.model.program import ConsolidateOut, Program, ProgramOut, ProgramRef, ProgramStart
from activity_monitor.services.events import drop_matched_focus_opens
from activity_monitor.services.programs import expire_stale, find_active_program
from activity_monitor.services.timelines import compose_segments, event_program_id, program_bounds, program_events
from activity_monitor.util.time import as_utc

router = APIRouter(prefix="/api")


@router.post("/programs/start", response_model=ProgramOut)
async def program_start(payload: ProgramStart, session: SessionDep, request: Request) -> ProgramOut:
    now = datetime.now(UTC)
    await expire_stale(session, now, stale_after(request))

    if await find_active_program(session, payload.program_id) is not None:
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
    program = await find_active_program(session, payload.program_id)

    if program is None:
        raise HTTPException(404, f"No active program '{payload.program_id}'")

    program.last_seen_at = datetime.now(UTC)
    await session.commit()

    return ProgramOut(program_id=program.program_id, source=program.source, host=program.host)


@router.post("/programs/end", response_model=ProgramOut)
async def program_end(payload: ProgramRef, session: SessionDep) -> ProgramOut:
    program = await find_active_program(session, payload.program_id)

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
    """Tidy the stored history.

    Finished programs are rewritten from their own events: a program with no events is
    deleted, each one is trimmed to its first and last non-idle boundary, and an idle
    gap longer than ``idle_seconds`` splits it into separate blocks so the dead time is
    not counted as presence. Running programs (``ended_at`` unset) are left alone.

    Desktop ``focus`` opens whose span already has a close are dropped too: the close
    carries the span's ``duration_ms``, so the open row only survives as the duplicate
    readers merge away (see ``_merge_focus_spans``). A trailing open with no close is
    the still-running span and is kept.
    """
    now = datetime.now(UTC)
    await expire_stale(session, now, stale_after(request))

    programs = list(
        (await session.execute(select(Program).where(Program.ended_at.is_not(None)).order_by(Program.started_at.asc())))
        .scalars()
        .all()
    )

    threshold = timedelta(seconds=idle_seconds)
    events = await program_events(session, {program.program_id for program in programs})
    by_pid: dict[str, list[ActivityEvent]] = {}

    for event in events:
        program_id = event_program_id(event)

        if program_id is not None:
            by_pid.setdefault(program_id, []).append(event)

    removed = trimmed = split = 0

    for program in programs:
        program_events_list = by_pid.get(program.program_id, [])

        if not program_events_list:
            await session.delete(program)
            removed += 1
            continue

        started, ended = program_bounds(program, now, now)
        in_range = [event for event in program_events_list if started <= as_utc(event.timestamp) <= ended]

        if not in_range:
            continue

        segments = compose_segments(
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

    dropped = await drop_matched_focus_opens(session)
    await session.commit()

    return ConsolidateOut(removed=removed, trimmed=trimmed, split=split, dropped_focus_opens=dropped)
