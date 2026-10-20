from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from activity_monitor.model.event import EventOut


class UserSpan(BaseModel):
    start: datetime
    end: datetime
    title: str | None = None


class UserLane(BaseModel):
    task: str
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


class ActorLane(BaseModel):
    actor: str
    events: list[EventOut] = []


class AgenticTimeline(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    lanes: list[ActorLane] = []
