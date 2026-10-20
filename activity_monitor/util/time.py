"""Timestamp normalization shared across sources, models and timelines."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()

    return value.astimezone(UTC)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return _to_utc(value)

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)

    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.fromtimestamp(int(text), tz=UTC)
        except ValueError:
            pass
        try:
            return _to_utc(datetime.fromisoformat(text))
        except ValueError:
            return None

    return None
