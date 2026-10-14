from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlmodel import JSON, Column, Field, SQLModel


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


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


class SourceInfo(BaseModel):
    name: str
    kind: str
    pull: bool


class StatsOut(BaseModel):
    events: int
    sources: int
    actors: int
    tasks: int


class ActorLane(BaseModel):
    actor: str
    events: list[EventOut] = []


class AgenticTimeline(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    lanes: list[ActorLane] = []


class TaskSummary(BaseModel):
    task: str
    count: int
    first: datetime
    last: datetime


class Program(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    program_id: str = Field(index=True)
    source: str = Field(index=True, default="opencode")
    host: str = Field(index=True, default="")
    actor: str = Field(index=True, default="opencode")
    started_at: datetime = Field(index=True)
    last_seen_at: datetime = Field(index=True)
    ended_at: datetime | None = Field(default=None, index=True)


class ProgramStart(BaseModel):
    program_id: str
    source: str = "opencode"
    host: str = ""
    actor: str | None = None


class ProgramRef(BaseModel):
    program_id: str


class ProgramOut(BaseModel):
    program_id: str
    source: str
    host: str


class ProgramSegment(BaseModel):
    start: datetime
    end: datetime
    state: str


class PresenceLane(BaseModel):
    key: str
    source: str
    host: str
    parallel_id: int
    segments: list[ProgramSegment] = []
    events: list[EventOut] = []


class PresenceTimeline(BaseModel):
    start: datetime
    end: datetime
    lanes: list[PresenceLane] = []


class UserSpan(BaseModel):
    start: datetime
    end: datetime
    title: str | None = None


class UserLane(BaseModel):
    process: str
    label: str
    spans: list[UserSpan] = []


class UserInterval(BaseModel):
    start: datetime
    end: datetime


class UserTimeline(BaseModel):
    start: datetime
    end: datetime
    lanes: list[UserLane] = []
    idle: list[UserInterval] = []
    locked: list[UserInterval] = []


class CodeTimeline(BaseModel):
    start: datetime
    end: datetime
    events: list[EventOut] = []


class ConsolidateOut(BaseModel):
    removed: int = 0
    trimmed: int = 0
    split: int = 0
