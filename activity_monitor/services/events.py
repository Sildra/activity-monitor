"""The single event write path, plus the focus-span helpers over stored rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from activity_monitor.model.event import ActivityEvent, EventOut
from activity_monitor.services.sources import UNKNOWN_TASK, USER_SOURCE, SourceProvider
from activity_monitor.util.time import as_utc

_TASK_SET_KIND = "task.set"
# A `heartbeat` is a liveness ping, not an activity event: it is never stored
# (see `store_events`) and only feeds the open-span staleness check in the user
# timeline. Legacy rows are still read for that check.
HEARTBEAT_KIND = "heartbeat"


async def _backfill_task(session: AsyncSession, program_id: str, host: str, task: str, at: datetime) -> None:
    """Retroactively tag a program's earlier untagged work after a ``task.set``.

    A ``/task`` command only pins the events that follow it (the plugin tags them as
    they happen), so work done before the task was set keeps the producer's default
    (``UNKNOWN_TASK``, or nothing at all for older producers). This walks the same
    program's events back from ``at`` and stamps them with ``task``, stopping at the
    previous ``task.set`` so an earlier task range is left intact.
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
            or_(ActivityEvent.task.is_(None), ActivityEvent.task == UNKNOWN_TASK),
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

        if canonical["kind"] == HEARTBEAT_KIND:
            # Liveness pings are frequent and carry nothing to show, so they are
            # not written to the database; only the latest one is remembered on
            # the provider so `user_timeline` can tell the watcher is alive.
            provider.last_heartbeat = canonical["timestamp"]
            continue

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


def _focus_open_pairs(events: list[ActivityEvent]) -> list[ActivityEvent]:
    """Return the ``focus`` opens consumed by a close, in ``(timestamp, id)`` order.

    The watcher writes a span as an open (no ``duration_ms``) followed by the close
    carrying ``duration_ms`` — and at a focus switch the close of the old span plus
    the open of the new one share a timestamp, with the close inserted first. Walking
    in that order therefore pairs exactly like ``merge_focus_spans`` does: an open is
    consumed by the next close, an open with no close is the live trailing span.
    """
    ordered = sorted(
        (event for event in events if event.source == USER_SOURCE and event.kind == "focus"),
        key=lambda event: (event.timestamp, event.id or 0),
    )
    matched: list[ActivityEvent] = []
    open_event: ActivityEvent | None = None

    for event in ordered:
        if event.duration_ms is None:
            open_event = event
            continue

        if open_event is not None:
            matched.append(open_event)
            open_event = None

    return matched


async def drop_matched_focus_opens(session: AsyncSession) -> int:
    """Delete every windows ``focus`` open whose span already has a close."""
    stmt = select(ActivityEvent).where(ActivityEvent.source == USER_SOURCE, ActivityEvent.kind == "focus")
    matched = _focus_open_pairs(list((await session.execute(stmt)).scalars().all()))

    for event in matched:
        await session.delete(event)

    return len(matched)


def merge_focus_spans(events: list[ActivityEvent]) -> list[EventOut]:
    """Collapse each windows focus open/close pair into a single span event.

    The desktop watcher records a span as an open (no ``duration_ms``) followed by a
    close (with ``duration_ms``). Read back verbatim, every span shows up twice; here
    they become one event dated at the span start and carrying the span's duration.
    Once consolidate has dropped the open, the close alone still yields that event,
    dated back by its ``duration_ms``.
    """
    ordered = sorted(events, key=lambda event: (event.timestamp, event.id or 0))
    merged: list[EventOut] = []
    open_event: ActivityEvent | None = None

    for event in ordered:
        if event.source != USER_SOURCE or event.kind != "focus":
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
            # The open row may have been tidied away by consolidate; the close
            # alone still knows where the span began.
            span = EventOut.from_event(event)
            span.timestamp = as_utc(event.timestamp) - timedelta(milliseconds=event.duration_ms)
            merged.append(span)

    if open_event is not None:
        merged.append(EventOut.from_event(open_event))

    merged.sort(key=lambda event: (event.timestamp, event.id or 0), reverse=True)
    return merged
