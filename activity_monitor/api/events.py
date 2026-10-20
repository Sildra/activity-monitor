from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy import distinct, func, select

from activity_monitor.api.deps import SessionDep
from activity_monitor.model.event import ActivityEvent, EventOut, StatsOut, TaskSummary
from activity_monitor.services.events import HEARTBEAT_KIND, merge_focus_spans
from activity_monitor.util.time import as_utc

router = APIRouter(prefix="/api")


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
        stmt = stmt.where(ActivityEvent.kind != HEARTBEAT_KIND)

    if start is not None:
        stmt = stmt.where(ActivityEvent.timestamp >= start)

    if end is not None:
        stmt = stmt.where(ActivityEvent.timestamp <= end)

    events = list((await session.execute(stmt.limit(limit))).scalars().all())

    return merge_focus_spans(events)


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
    rows = (await session.execute(select(ActivityEvent.actor).distinct().order_by(ActivityEvent.actor))).scalars().all()

    return list(rows)


@router.get("/kinds", response_model=list[str])
async def list_kinds(session: SessionDep) -> list[str]:
    rows = (await session.execute(select(ActivityEvent.kind).distinct().order_by(ActivityEvent.kind))).scalars().all()

    return list(rows)


@router.get("/stats", response_model=StatsOut)
async def stats(session: SessionDep) -> StatsOut:
    total = (
        await session.execute(
            select(func.count()).select_from(ActivityEvent).where(ActivityEvent.kind != HEARTBEAT_KIND)
        )
    ).scalar_one()
    sources = (await session.execute(select(func.count(distinct(ActivityEvent.source))))).scalar_one()
    actors = (await session.execute(select(func.count(distinct(ActivityEvent.actor))))).scalar_one()
    tasks = (await session.execute(select(func.count(distinct(ActivityEvent.task))))).scalar_one()

    return StatsOut(events=total, sources=sources, actors=actors, tasks=tasks)
