"""Tests for the Windows watcher's sleep / clock-skew detection."""

from __future__ import annotations

import base64
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest

from activity_monitor.util.process_config import parse_process_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only watcher")

BASE = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)


def load_watcher_module() -> Any:
    from activity_monitor.services import windows_watcher

    return windows_watcher


def make_watcher(events: list[dict[str, Any]], **kwargs: Any) -> Any:
    windows_watcher = load_watcher_module()
    sink: Callable[[dict[str, Any]], bool] = lambda payload: events.append(payload) or True

    # A large heartbeat keeps unrelated tests free of liveness pings.
    return windows_watcher.WindowsWatcher(sink, skew_seconds=30.0, heartbeat=10_000.0, **kwargs)


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


# -- heartbeat ----------------------------------------------------------------


def test_heartbeat_reports_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    sink: Callable[[dict[str, Any]], bool] = lambda payload: events.append(payload) or True
    watcher = windows_watcher.WindowsWatcher(sink, heartbeat=10.0)
    desktop = FakeDesktop(monkeypatch, windows_watcher)

    watcher.tick(desktop.wall)  # sets the baseline; no ping yet
    desktop.advance(wall=5)
    watcher.tick(desktop.wall)  # still within the heartbeat window
    desktop.advance(wall=5)
    watcher.tick(desktop.wall)  # 10s elapsed -> one ping

    beats = [event for event in events if event["kind"] == "heartbeat"]
    assert len(beats) == 1
    assert beats[0]["timestamp"] == (BASE + timedelta(seconds=10)).isoformat(timespec="milliseconds")


# -- event task ---------------------------------------------------------------


def test_task_env_sets_focus_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVITY_MONITOR_TASK", "JIRA-123")
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(BASE)

    focus = [event for event in events if event["kind"] == "focus"]
    assert [event["task"] for event in focus] == ["JIRA-123"]


def test_focus_task_defaults_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACTIVITY_MONITOR_TASK", raising=False)
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(BASE)

    focus = [event for event in events if event["kind"] == "focus"]
    # A task is a unit of work, never a process: focus reports the default task
    # and keeps the foreground process as event data instead.
    assert [event["task"] for event in focus] == ["Unknown"]
    assert [event["process"] for event in focus] == ["code.exe"]
    # The image path / pid never travel: `process` is the whole identity.
    assert all("detail" not in event for event in focus)


def test_process_config_rewrites_focus_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # The blanket fallback task is set to prove a configured task wins over it.
    monkeypatch.setenv("ACTIVITY_MONITOR_TASK", "JIRA-9")
    rules = parse_process_config(
        {"process": {"input": {"name": "code"}, "output": {"name": "[redacted]", "title": "editor", "task": "ide"}}},
        source="test",
    )
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events, process_rules=rules)
    FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(BASE)

    focus = [event for event in events if event["kind"] == "focus"]
    assert [(event["task"], event["process"], event["title"]) for event in focus] == [("ide", "[redacted]", "editor")]
    # Redaction needs no `detail`: focus never carries an image path or pid, so
    # the rewritten basename in `process` cannot leak the original program.
    assert all("detail" not in event for event in focus)


# -- event actor --------------------------------------------------------------


def test_display_user_env_sets_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVITY_MONITOR_DISPLAY_USER", "sildr")
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(BASE)

    assert {event["actor"] for event in events} == {"sildr"}


def test_actor_defaults_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACTIVITY_MONITOR_DISPLAY_USER", raising=False)
    events: list[dict[str, Any]] = []
    watcher = make_watcher(events)
    FakeDesktop(monkeypatch, load_watcher_module())

    watcher.tick(BASE)

    assert {event["actor"] for event in events} == {"user"}


def test_explicit_actor_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVITY_MONITOR_DISPLAY_USER", "sildr")
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    sink: Callable[[dict[str, Any]], bool] = lambda payload: events.append(payload) or True
    watcher = windows_watcher.WindowsWatcher(sink, actor="robot", skew_seconds=30.0)
    FakeDesktop(monkeypatch, windows_watcher)

    watcher.tick(BASE)

    assert {event["actor"] for event in events} == {"robot"}


# -- command line options -----------------------------------------------------


def test_parse_args_defaults() -> None:
    args = load_watcher_module().parse_args([])

    assert args.base_url is None
    assert args.source == "windows"
    assert args.actor is None
    assert args.host is None
    assert args.task is None
    assert args.username is None
    assert args.password is None
    assert args.ca_cert is None
    assert args.client_cert is None
    assert args.client_key is None
    assert args.insecure is False
    assert args.heartbeat_seconds == 60.0
    assert args.tray is False


def test_parse_args_overrides() -> None:
    args = load_watcher_module().parse_args(
        [
            "--base-url",
            "https://example:9000",
            "--source",
            "desktop",
            "--actor",
            "sildr",
            "--host",
            "laptop",
            "--task",
            "JIRA-1",
            "--username",
            "alice",
            "--password",
            "s3cret",
            "--ca-cert",
            "ca.pem",
            "--client-cert",
            "client.pem",
            "--client-key",
            "client.key",
            "--heartbeat-seconds",
            "5",
            "--tray",
            "--insecure",
        ]
    )

    assert args.base_url == "https://example:9000"
    assert args.source == "desktop"
    assert args.actor == "sildr"
    assert args.host == "laptop"
    assert args.task == "JIRA-1"
    assert args.username == "alice"
    assert args.password == "s3cret"
    assert args.ca_cert == "ca.pem"
    assert args.client_cert == "client.pem"
    assert args.client_key == "client.key"
    assert args.insecure is True
    assert args.heartbeat_seconds == 5.0
    assert args.tray is True


def test_host_option_sets_event_host(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    sink: Callable[[dict[str, Any]], bool] = lambda payload: events.append(payload) or True
    watcher = windows_watcher.WindowsWatcher(sink, host="laptop")
    FakeDesktop(monkeypatch, windows_watcher)

    watcher.tick(BASE)

    assert {event["host"] for event in events} == {"laptop"}


def test_task_option_sets_focus_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACTIVITY_MONITOR_TASK", raising=False)
    windows_watcher = load_watcher_module()
    events: list[dict[str, Any]] = []
    sink: Callable[[dict[str, Any]], bool] = lambda payload: events.append(payload) or True
    watcher = windows_watcher.WindowsWatcher(sink, task="JIRA-9")
    FakeDesktop(monkeypatch, windows_watcher)

    watcher.tick(BASE)

    focus = [event for event in events if event["kind"] == "focus"]
    assert [event["task"] for event in focus] == ["JIRA-9"]


def test_auth_header_prefers_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    monkeypatch.setenv("ACTIVITY_MONITOR_USERNAME", "env")
    monkeypatch.setenv("ACTIVITY_MONITOR_PASSWORD", "envpass")

    header = windows_watcher.build_auth_header("cli", "clipass")

    token = base64.b64encode(b"cli:clipass").decode("ascii")
    assert header == f"Basic {token}"


def test_auth_header_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    monkeypatch.setenv("ACTIVITY_MONITOR_USERNAME", "env")
    monkeypatch.setenv("ACTIVITY_MONITOR_PASSWORD", "envpass")

    header = windows_watcher.build_auth_header()

    token = base64.b64encode(b"env:envpass").decode("ascii")
    assert header == f"Basic {token}"


def test_auth_header_none_without_username(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    monkeypatch.delenv("ACTIVITY_MONITOR_USERNAME", raising=False)

    assert windows_watcher.build_auth_header() is None


# -- sink status reporting (tray icon) ----------------------------------------


class FakeResponse:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_http_sink_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    states: list[bool] = []
    monkeypatch.setattr(windows_watcher.request, "urlopen", lambda *args, **kwargs: FakeResponse())
    sink = windows_watcher.HttpSink("http://localhost:8001", "windows", on_status=states.append)

    assert sink({"kind": "heartbeat"}) is True
    assert states == [True]


def test_http_sink_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_watcher = load_watcher_module()
    states: list[bool] = []

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(windows_watcher.request, "urlopen", refuse)
    sink = windows_watcher.HttpSink("http://localhost:8001", "windows", on_status=states.append)

    assert sink({"kind": "heartbeat"}) is False
    assert states == [False]
