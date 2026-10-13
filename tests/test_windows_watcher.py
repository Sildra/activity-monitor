"""Tests for the Windows watcher's sleep / clock-skew detection."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only watcher")

BASE = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)


def load_watcher_module() -> Any:
    from activity_monitor import windows_watcher

    return windows_watcher


def make_watcher(events: list[dict[str, Any]]) -> Any:
    windows_watcher = load_watcher_module()
    sink: Callable[[dict[str, Any]], bool] = lambda payload: events.append(payload) or True

    return windows_watcher.WindowsWatcher(sink, skew_seconds=30.0)


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["kind"] for event in events]


class FakeDesktop:
    """Scripts the watcher's Windows probes so `tick` can be driven deterministically."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, windows_watcher: Any) -> None:
        self.wall = BASE
        self.unbiased = 1000.0
        self.locked = False
        self.fg: dict[str, Any] | None = {
            "hwnd": 111,
            "title": "Editor",
            "process": "code.exe",
            "image": "code.exe",
            "pid": 42,
        }

        monkeypatch.setattr(windows_watcher, "unbiased_time", lambda: self.unbiased)
        monkeypatch.setattr(windows_watcher, "is_locked", lambda: self.locked)
        monkeypatch.setattr(windows_watcher, "foreground_window", lambda: self.fg)
        monkeypatch.setattr(windows_watcher, "idle_millis", lambda: 0)

    def advance(self, *, wall: float = 0.0, unbiased: float | None = None, locked: bool | None = None) -> None:
        self.wall += timedelta(seconds=wall)
        self.unbiased += wall if unbiased is None else unbiased

        if locked is not None:
            self.locked = locked


# -- detect_sleep unit tests -------------------------------------------------


def test_sleep_skew_enters_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    times = iter([1000.0, 1005.0])

    monkeypatch.setattr(windows_watcher, "unbiased_time", lambda: next(times))

    watcher.detect_sleep(BASE)
    watcher.detect_sleep(BASE + timedelta(seconds=60))

    # The wall clock advanced 60s while only 5s of working time elapsed.
    assert kinds(events) == ["lock"]
    assert events[0]["timestamp"] == BASE.isoformat(timespec="milliseconds")
    assert watcher._was_locked is True
    assert watcher._lock_start == BASE


def test_no_skew_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    times = iter([1000.0, 1005.0])

    monkeypatch.setattr(windows_watcher, "unbiased_time", lambda: next(times))

    watcher.detect_sleep(BASE)
    watcher.detect_sleep(BASE + timedelta(seconds=5))

    assert events == []


def test_first_tick_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)

    monkeypatch.setattr(windows_watcher, "unbiased_time", lambda: 1000.0)
    watcher.detect_sleep(BASE)

    assert events == []


def test_sleep_while_locked_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    watcher._was_locked = True
    times = iter([1000.0, 1005.0])

    monkeypatch.setattr(windows_watcher, "unbiased_time", lambda: next(times))

    watcher.detect_sleep(BASE)
    watcher.detect_sleep(BASE + timedelta(seconds=60))

    assert events == []


def test_missing_unbiased_clock_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)

    monkeypatch.setattr(windows_watcher, "unbiased_time", lambda: None)

    watcher.detect_sleep(BASE)
    watcher.detect_sleep(BASE + timedelta(seconds=600))

    assert events == []


# -- lock / sleep interaction -------------------------------------------------


def test_sleep_while_unlocked_locks_until_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    desktop = FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(desktop.wall)
    desktop.advance(wall=3600, unbiased=1.0)  # slept an hour, woke still unlocked
    watcher.tick(desktop.wall)

    assert kinds(events) == ["focus", "focus", "lock", "unlock", "focus"]
    assert "suspended" in events[2]["detail"]
    assert events[3]["duration_ms"] == pytest.approx(3_600_000.0)


def test_sleep_that_locks_the_session_extends_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    desktop = FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(desktop.wall)
    desktop.locked = True
    desktop.advance(wall=3600, unbiased=1.0)  # slept, woke to the lock screen
    watcher.tick(desktop.wall)
    desktop.advance(wall=30, locked=False)  # the user unlocks
    watcher.tick(desktop.wall)

    assert kinds(events) == ["focus", "focus", "lock", "unlock", "focus"]
    assert "suspended" in events[2]["detail"]
    # The sleep lock runs from the sleep start until the real unlock, not the wake.
    assert events[3]["duration_ms"] == pytest.approx(3_630_000.0)


def test_lock_before_sleep_is_not_doubled(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    desktop = FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(desktop.wall)
    desktop.advance(wall=1, locked=True)
    watcher.tick(desktop.wall)  # the session locks first
    locked_at = desktop.wall
    desktop.advance(wall=3600, unbiased=1.0)  # then the machine sleeps
    watcher.tick(desktop.wall)
    desktop.advance(wall=5, locked=False)
    watcher.tick(desktop.wall)

    locks = [event for event in events if event["kind"] == "lock"]
    assert len(locks) == 1
    assert "detail" not in locks[0]
    assert locks[0]["timestamp"] == locked_at.isoformat(timespec="milliseconds")

    unlock = next(event for event in events if event["kind"] == "unlock")
    assert unlock["duration_ms"] == pytest.approx(3_605_000.0)


# -- event task ---------------------------------------------------------------


def test_task_env_sets_focus_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVITY_MONITOR_TASK", "JIRA-123")
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(BASE)

    focus = [event for event in events if event["kind"] == "focus"]
    assert [event["task"] for event in focus] == ["JIRA-123"]


def test_focus_task_defaults_to_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACTIVITY_MONITOR_TASK", raising=False)
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(BASE)

    focus = [event for event in events if event["kind"] == "focus"]
    assert [event["task"] for event in focus] == ["code.exe"]
