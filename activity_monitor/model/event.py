from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlmodel import JSON, Column, Field, SQLModel

from activity_monitor.util.time import as_utc


class ActivityEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    source: str = Field(index=True)
    kind: str = Field(index=True)
    actor: str = Field(index=True, default="user")
    host: str = Field(index=True, default="")
    task: str | None = Field(default=None, index=True)
    timestamp: datetime = Field(index=True)

    duration_ms: int | None = None
    title: str | None = None
    detail: str | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    data: dict = Field(default_factory=dict, sa_column=Column(JSON))

    dedup_key: str = Field(unique=True, index=True)


class EventOut(BaseModel):
    id: int | None = None
    source: str
    kind: str
    actor: str
    host: str = ""
    task: str | None = None
    timestamp: datetime
    duration_ms: int | None = None
    title: str | None = None
    detail: str | None = None
    tags: list[str] = []
    data: dict = {}

    @classmethod
    def from_event(cls, event: ActivityEvent) -> EventOut:
        return cls(
            id=event.id,
            source=event.source,
            kind=event.kind,
            actor=event.actor,
            host=event.host or "",
            task=event.task,
            duration_ms=event.duration_ms,
            title=event.title,
            detail=event.detail,
            tags=event.tags or [],
            data=event.data or {},
            timestamp=as_utc(event.timestamp),
        )


class StatsOut(BaseModel):
    events: int
    sources: int
    actors: int
    tasks: int


class TaskSummary(BaseModel):
    task: str
    count: int
    first: datetime
    last: datetime
