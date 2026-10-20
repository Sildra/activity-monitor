from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlmodel import Field, SQLModel

from activity_monitor.model.event import EventOut


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


class ConsolidateOut(BaseModel):
    removed: int = 0
    trimmed: int = 0
    split: int = 0
    dropped_focus_opens: int = 0
