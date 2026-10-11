from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from activity_monitor.main import create_app
from activity_monitor.models import as_utc


def make_client(tmp_path: Path, extra: str = "", stale: int | None = None) -> TestClient:
    general = f"  database: {(tmp_path / 'activity.db').as_posix()}"

    if stale is not None:
        general += f"\n  stale_after_seconds: {stale}"

    config = tmp_path / "config.yaml"
    config.write_text(f"general:\n{general}\n{extra}", encoding="utf-8")

    args = SimpleNamespace(config=config, database=None, port=None, openapi=None)

    return TestClient(create_app(args))


PUSH_SOURCE = "sources:\n  opencode:\n    kind: Push\n    enabled: true\n"
GIT_SOURCE = "sources:\n  git:\n    kind: GitHistory\n    enabled: true\n    repositories: []\n"


def test_ingest_dedup_and_stats(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        event = {"id": "1", "kind": "tool_call", "actor": "agent-a", "task": "feature-x", "title": "edited"}

        assert client.post("/api/ingest/opencode", json=event).json() == {"accepted": 1}
        assert client.post("/api/ingest/opencode", json=event).json() == {"accepted": 0}

        stats = client.get("/api/stats").json()
        assert stats == {"events": 1, "sources": 1, "actors": 1, "tasks": 1}


def test_git_push_registers_branch_and_commit(tmp_path: Path) -> None:
    with make_client(tmp_path, GIT_SOURCE) as client:
        push = {
            "kind": "push",
            "repository": "/repo",
            "branch": "main",
            "reference": "refs/heads/main",
            "commit": "abc123",
            "previous": "def456",
            "remote": "origin",
            "url": "git@example.com:repo.git",
            "author": "Jane",
            "timestamp": "2026-01-01T00:00:00Z",
        }

        assert client.post("/api/ingest/git", json=push).json() == {"accepted": 1}
        assert client.post("/api/ingest/git", json=push).json() == {"accepted": 0}

        pushes = client.get("/api/tasks", params={"kind": "push"}).json()
        assert len(pushes) == 1
        assert pushes[0]["data"]["branch"] == "main"
        assert pushes[0]["data"]["commit"] == "abc123"
        assert pushes[0]["data"]["remote"] == "origin"

        commit = {
            "kind": "commit",
            "repository": "/repo",
            "commit": "abc123",
            "message": "hi",
            "author": "Jane",
            "timestamp": "2026-01-01T00:00:01Z",
        }

        assert client.post("/api/ingest/git", json=commit).json() == {"accepted": 1}
        assert {event["kind"] for event in client.get("/api/tasks").json()} == {"push", "commit"}


def test_git_task_derived_from_pattern(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK_PATTERN", r"JIRA-[0-9]+")

    with make_client(tmp_path, GIT_SOURCE) as client:
        events = [
            # commit message wins over the branch when both match
            {
                "kind": "commit",
                "repository": "/repo",
                "commit": "c1",
                "message": "JIRA-123 add login",
                "branch": "feature/JIRA-456",
                "author": "Jane",
                "timestamp": "2026-01-01T00:00:00Z",
            },
            # no message -> falls back to the branch
            {
                "kind": "push",
                "repository": "/repo",
                "branch": "feature/JIRA-456-thing",
                "reference": "refs/heads/feature/JIRA-456-thing",
                "commit": "c2",
                "remote": "origin",
                "author": "Jane",
                "timestamp": "2026-01-01T00:01:00Z",
            },
            # no match anywhere -> falls back to the repository name
            {
                "kind": "commit",
                "repository": "/repo",
                "commit": "c3",
                "message": "no ticket here",
                "author": "Jane",
                "timestamp": "2026-01-01T00:02:00Z",
            },
        ]

        assert client.post("/api/ingest/git", json={"events": events}).json() == {"accepted": 3}

        tasks = {event["data"]["commit"]: event["task"] for event in client.get("/api/tasks").json()}
        assert tasks == {"c1": "JIRA-123", "c2": "JIRA-456", "c3": "repo"}


def test_code_timeline_returns_commits_and_pushes(tmp_path: Path) -> None:
    with make_client(tmp_path, GIT_SOURCE) as client:
        events = [
            {
                "kind": "commit",
                "repository": "/repo",
                "commit": "c1",
                "message": "first",
                "author": "Jane",
                "timestamp": "2026-02-01T10:00:00Z",
            },
            {
                "kind": "push",
                "repository": "/repo",
                "branch": "main",
                "reference": "refs/heads/main",
                "commit": "c1",
                "remote": "origin",
                "author": "Jane",
                "timestamp": "2026-02-01T10:05:00Z",
            },
        ]

        assert client.post("/api/ingest/git", json={"events": events}).json() == {"accepted": 2}

        body = client.get(
            "/api/timelines/code", params={"start": "2026-02-01T00:00:00Z", "end": "2026-02-02T00:00:00Z"}
        ).json()

        assert [event["kind"] for event in body["events"]] == ["commit", "push"]
        assert body["events"][1]["data"]["remote"] == "origin"

        empty = client.get(
            "/api/timelines/code", params={"start": "2026-03-01T00:00:00Z", "end": "2026-03-02T00:00:00Z"}
        ).json()
        assert empty["events"] == []


def test_agentic_timeline_groups_by_actor(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post(
            "/api/ingest/opencode",
            json={
                "events": [
                    {"id": "a", "actor": "agent-a", "kind": "edit", "title": "one"},
                    {"id": "b", "actor": "agent-b", "kind": "test", "title": "two"},
                    {"id": "c", "actor": "agent-a", "kind": "edit", "title": "three"},
                ]
            },
        )

        lanes = client.get("/api/timelines/agentic").json()["lanes"]

        assert [(lane["actor"], len(lane["events"])) for lane in lanes] == [("agent-a", 2), ("agent-b", 1)]


def test_task_search_and_summary(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post(
            "/api/ingest/opencode",
            json={
                "events": [
                    {"id": "a", "actor": "user", "task": "feature-x", "title": "wrote parser"},
                    {"id": "b", "actor": "user", "task": "feature-y", "title": "wrote tests"},
                ]
            },
        )

        summary = client.get("/api/tasks/summary").json()
        assert {item["task"] for item in summary} == {"feature-x", "feature-y"}

        results = client.get("/api/tasks", params={"q": "tests"}).json()
        assert [event["title"] for event in results] == ["wrote tests"]


def test_unknown_source_is_404(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        assert client.post("/api/ingest/missing", json={"title": "x"}).status_code == 404


def test_pull_source_cannot_be_fetched_when_push(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        assert client.post("/api/sources/opencode/fetch").status_code == 400


@pytest.mark.parametrize("endpoint", ["/api/sources", "/api/stats", "/api/actors", "/api/kinds"])
def test_basic_endpoints(tmp_path: Path, endpoint: str) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        assert client.get(endpoint).status_code == 200


def test_concurrent_programs_split_lanes_and_reuse(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        base = {"source": "opencode", "host": "h1"}

        client.post("/api/programs/start", json={"program_id": "p1", **base})
        client.post("/api/programs/start", json={"program_id": "p2", **base})

        lanes = client.get("/api/timelines/presence").json()["lanes"]
        assert [lane["key"] for lane in lanes] == ["opencode-h1-0", "opencode-h1-1"]

        client.post("/api/programs/end", json={"program_id": "p1"})
        client.post("/api/programs/start", json={"program_id": "p3", **base})

        lanes = client.get("/api/timelines/presence").json()["lanes"]
        assert [lane["key"] for lane in lanes] == ["opencode-h1-0", "opencode-h1-1"]


def test_program_beat_keeps_alive_and_404_after_end(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post("/api/programs/start", json={"program_id": "p1", "source": "opencode", "host": "h1"})

        assert client.post("/api/programs/start", json={"program_id": "p1"}).status_code == 409
        assert client.post("/api/programs/beat", json={"program_id": "p1"}).status_code == 200
        assert client.post("/api/programs/end", json={"program_id": "p1"}).status_code == 200
        assert client.post("/api/programs/end", json={"program_id": "p1"}).status_code == 404


def test_program_end_and_beat_timestamps_are_aware_utc(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post("/api/programs/start", json={"program_id": "p1", "source": "opencode", "host": "h1"})

        before = datetime.now(UTC)
        client.post("/api/programs/beat", json={"program_id": "p1"})
        client.post("/api/programs/end", json={"program_id": "p1"})
        after = datetime.now(UTC)

        conn = sqlite3.connect(tmp_path / "activity.db")
        last_seen, ended = conn.execute("select last_seen_at, ended_at from program").fetchone()
        conn.close()

        assert last_seen is not None and ended is not None
        for stored in (last_seen, ended):
            stored_dt = as_utc(datetime.fromisoformat(stored))
            assert before <= stored_dt <= after


def test_stale_program_detected_and_restart_merges_same_lane(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE, stale=1) as client:
        client.post("/api/programs/start", json={"program_id": "p1", "source": "opencode", "host": "h1"})

        time.sleep(1.3)
        client.post("/api/programs/start", json={"program_id": "p2", "source": "opencode", "host": "h1"})

        lanes = client.get("/api/timelines/presence").json()["lanes"]
        assert [lane["key"] for lane in lanes] == ["opencode-h1-0"]


def test_presence_timeline_persists_stale_program_end(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE, stale=1) as client:
        client.post("/api/programs/start", json={"program_id": "p1", "source": "opencode", "host": "h1"})

        time.sleep(1.3)
        start = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        end = (datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        client.get("/api/timelines/presence", params={"start": start, "end": end})

        conn = sqlite3.connect(tmp_path / "activity.db")
        started, last_seen, ended = conn.execute(
            "select started_at, last_seen_at, ended_at from program where program_id = 'p1'"
        ).fetchone()
        conn.close()

        assert ended is not None
        assert datetime.fromisoformat(ended) == datetime.fromisoformat(last_seen)
        assert datetime.fromisoformat(started) <= datetime.fromisoformat(ended)


def test_presence_timeline_merges_lanes_and_attaches_events(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        db_path = tmp_path / "activity.db"
        t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        def program(program_id: str, start: datetime, end: datetime) -> None:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "insert into program (program_id, source, host, actor, started_at, last_seen_at, ended_at)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                (program_id, "opencode", "h1", "opencode", fmt(start), fmt(end), fmt(end)),
            )
            conn.commit()
            conn.close()

        program("p1", t0, t0 + timedelta(minutes=30))
        program("p2", t0 + timedelta(minutes=60), t0 + timedelta(minutes=90))
        program("p3", t0 + timedelta(minutes=15), t0 + timedelta(minutes=45))

        client.post(
            "/api/ingest/opencode",
            json={
                "id": "e1",
                "kind": "prompt.begin",
                "actor": "user",
                "title": "x",
                "timestamp": fmt(t0 + timedelta(minutes=65)),
                "program": {"id": "p2", "source": "opencode", "host": "h1"},
            },
        )

        body = client.get(
            "/api/timelines/presence", params={"start": fmt(t0), "end": fmt(t0 + timedelta(hours=2))}
        ).json()

        assert [lane["key"] for lane in body["lanes"]] == ["opencode-h1-0", "opencode-h1-1"]

        lane0, lane1 = body["lanes"]
        assert [seg["state"] for seg in lane0["segments"]] == ["idle", "closed", "idle", "active", "closed"]
        assert [seg["state"] for seg in lane1["segments"]] == ["closed", "idle", "closed"]

        idle0 = lane0["segments"][0]
        assert datetime.fromisoformat(idle0["end"]) - datetime.fromisoformat(idle0["start"]) == timedelta(minutes=30)

        active = lane0["segments"][3]
        assert datetime.fromisoformat(active["start"]) == datetime.fromisoformat(lane0["segments"][2]["end"])

        assert [event["title"] for event in lane0["events"]] == ["x"]
        assert lane1["events"] == []


def test_presence_multiple_prompts_toggle_state(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        db_path = tmp_path / "activity.db"
        t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        conn = sqlite3.connect(db_path)
        conn.execute(
            "insert into program (program_id, source, host, actor, started_at, last_seen_at, ended_at)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            ("p1", "opencode", "h1", "opencode", fmt(t0), fmt(t0 + timedelta(hours=1)), fmt(t0 + timedelta(hours=1))),
        )
        conn.commit()
        conn.close()

        def prompt(pid: str, msg: str, kind: str, minutes: int) -> None:
            client.post(
                "/api/ingest/opencode",
                json={
                    "id": pid,
                    "kind": kind,
                    "actor": "user",
                    "timestamp": fmt(t0 + timedelta(minutes=minutes)),
                    "data": {"sessionID": "sess-1", "messageID": msg},
                    "program": {"id": "p1", "source": "opencode", "host": "h1"},
                },
            )

        prompt("a", "m1", "prompt.begin", 10)
        prompt("b", "m1", "prompt.end", 20)
        prompt("c", "m2", "prompt.begin", 25)
        prompt("d", "m3", "prompt.begin", 30)
        prompt("e", "m3", "prompt.end", 35)
        prompt("f", "m4", "prompt.begin", 50)

        body = client.get(
            "/api/timelines/presence", params={"start": fmt(t0), "end": fmt(t0 + timedelta(hours=1))}
        ).json()

        lane = body["lanes"][0]
        assert [seg["state"] for seg in lane["segments"]] == ["idle", "active", "idle", "active", "idle", "active"]

        def bound(index: int) -> tuple[datetime, datetime]:
            start = datetime.fromisoformat(lane["segments"][index]["start"])
            end = datetime.fromisoformat(lane["segments"][index]["end"])
            return start, end

        assert bound(1) == (t0 + timedelta(minutes=10), t0 + timedelta(minutes=20))
        assert bound(3) == (t0 + timedelta(minutes=25), t0 + timedelta(minutes=35))
        assert bound(4) == (t0 + timedelta(minutes=35), t0 + timedelta(minutes=50))
        assert bound(5) == (t0 + timedelta(minutes=50), t0 + timedelta(hours=1))

        assert [event["kind"] for event in lane["events"]] == [
            "prompt.begin",
            "prompt.end",
            "prompt.begin",
            "prompt.begin",
            "prompt.end",
            "prompt.begin",
        ]


def test_presence_timeline_with_aware_utc_query(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post("/api/programs/start", json={"program_id": "p1", "source": "opencode", "host": "h1"})

        client.post(
            "/api/ingest/opencode",
            json={
                "id": "e1",
                "kind": "prompt.begin",
                "actor": "user",
                "title": "x",
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "program": {"id": "p1", "source": "opencode", "host": "h1"},
            },
        )

        start = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        end = (datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")

        body = client.get("/api/timelines/presence", params={"start": start, "end": end}).json()

        assert [lane["key"] for lane in body["lanes"]] == ["opencode-h1-0"]

        lane = body["lanes"][0]
        assert any(segment["state"] == "active" for segment in lane["segments"])
        assert [event["title"] for event in lane["events"]] == ["x"]

        assert datetime.fromisoformat(body["end"]) > datetime.fromisoformat(body["start"])


WINDOWS_SOURCE = "sources:\n  windows:\n    kind: Push\n    enabled: true\n"


def test_user_timeline_reports_focus_lanes_and_states(tmp_path: Path) -> None:
    with make_client(tmp_path, WINDOWS_SOURCE) as client:
        t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        def send(payload: dict) -> None:
            assert client.post("/api/ingest/windows", json=payload).status_code == 200

        send({"id": "1", "kind": "focus", "task": "code", "title": "editor", "timestamp": fmt(t0)})
        send(
            {
                "id": "2",
                "kind": "focus",
                "task": "code",
                "title": "editor",
                "timestamp": fmt(t0 + timedelta(minutes=10)),
                "duration_ms": 600_000,
            }
        )
        send(
            {
                "id": "3",
                "kind": "focus",
                "task": "browser",
                "title": "docs",
                "timestamp": fmt(t0 + timedelta(minutes=10)),
            }
        )
        send({"id": "4", "kind": "idle.start", "timestamp": fmt(t0 + timedelta(minutes=15))})
        send({"id": "5", "kind": "idle.end", "timestamp": fmt(t0 + timedelta(minutes=20)), "duration_ms": 300_000})
        send(
            {
                "id": "6",
                "kind": "focus",
                "task": "browser",
                "title": "docs",
                "timestamp": fmt(t0 + timedelta(minutes=20)),
                "duration_ms": 600_000,
            }
        )

        body = client.get(
            "/api/timelines/user", params={"start": fmt(t0), "end": fmt(t0 + timedelta(minutes=30))}
        ).json()

        lanes = {lane["process"]: lane for lane in body["lanes"]}
        assert set(lanes) == {"code", "browser"}
        assert len(lanes["code"]["spans"]) == 1
        assert len(lanes["browser"]["spans"]) == 1

        code_span = lanes["code"]["spans"][0]
        assert datetime.fromisoformat(code_span["start"]) == t0
        assert datetime.fromisoformat(code_span["end"]) == t0 + timedelta(minutes=10)

        browser_span = lanes["browser"]["spans"][0]
        assert datetime.fromisoformat(browser_span["start"]) == t0 + timedelta(minutes=10)
        assert datetime.fromisoformat(browser_span["end"]) == t0 + timedelta(minutes=20)

        assert len(body["idle"]) == 1
        assert datetime.fromisoformat(body["idle"][0]["start"]) == t0 + timedelta(minutes=15)
        assert datetime.fromisoformat(body["idle"][0]["end"]) == t0 + timedelta(minutes=20)
        assert body["locked"] == []


def test_user_timeline_open_span_runs_to_query_end(tmp_path: Path) -> None:
    with make_client(tmp_path, WINDOWS_SOURCE) as client:
        t0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        client.post("/api/ingest/windows", json={"id": "1", "kind": "focus", "task": "code", "timestamp": fmt(t0)})

        end = t0 + timedelta(minutes=20)
        body = client.get("/api/timelines/user", params={"start": fmt(t0), "end": fmt(end)}).json()

        span = body["lanes"][0]["spans"][0]
        assert datetime.fromisoformat(span["start"]) == t0
        assert datetime.fromisoformat(span["end"]) == end


def test_tasks_merges_windows_focus_spans(tmp_path: Path) -> None:
    with make_client(tmp_path, WINDOWS_SOURCE) as client:
        t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        def send(payload: dict) -> None:
            assert client.post("/api/ingest/windows", json=payload).status_code == 200

        send({"id": "1", "kind": "focus", "task": "code", "title": "editor", "timestamp": fmt(t0)})
        send(
            {
                "id": "2",
                "kind": "focus",
                "task": "code",
                "title": "editor",
                "timestamp": fmt(t0 + timedelta(minutes=10)),
                "duration_ms": 600_000,
            }
        )
        send(
            {
                "id": "3",
                "kind": "focus",
                "task": "code",
                "title": "editor",
                "timestamp": fmt(t0 + timedelta(minutes=10)),
            }
        )

        events = client.get("/api/tasks").json()

        assert len(events) == 2

        live, span = events
        assert live["duration_ms"] is None
        assert datetime.fromisoformat(live["timestamp"]) == t0 + timedelta(minutes=10)

        assert span["duration_ms"] == 600_000
        assert datetime.fromisoformat(span["timestamp"]) == t0
