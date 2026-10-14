from __future__ import annotations

import socket
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from activity_monitor.main import create_app
from activity_monitor.models import as_utc


def make_client(
    tmp_path: Path, extra: str = "", stale: int | None = None, basic_auth: tuple[str, str] | None = None
) -> TestClient:
    general = f"  database: {(tmp_path / 'activity.db').as_posix()}"

    if stale is not None:
        general += f"\n  stale_after_seconds: {stale}"

    if basic_auth is not None:
        username, password = basic_auth
        general += f"\n  basic_auth_username: {username}\n  basic_auth_password: {password}"

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


def test_task_set_backfills_program_work_until_previous_task(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds").replace("+00:00", "Z")

        def ingest(
            event_id: str,
            kind: str,
            seconds: int,
            *,
            title: str | None = None,
            task: str | None = None,
            program: str = "p1",
        ) -> None:
            body: dict[str, object] = {
                "id": event_id,
                "kind": kind,
                "actor": "opencode",
                "timestamp": fmt(t0 + timedelta(seconds=seconds)),
                "program": {"id": program, "source": "opencode", "host": "h1"},
            }

            if title is not None:
                body["title"] = title

            if task is not None:
                body["task"] = task

            assert client.post("/api/ingest/opencode", json=body).json() == {"accepted": 1}

        def task_of(title: str) -> str | None:
            return next(event["task"] for event in client.get("/api/tasks").json() if event["title"] == title)

        # Work done before the first /task is untagged and only pinned afterwards.
        ingest("before", "prompt.begin", 10, title="before")
        ingest("before-2", "prompt.begin", 20, title="before-2")
        ingest("ts1", "task.set", 30, task="AAA")
        ingest("under-a", "prompt.begin", 40, title="under-a", task="AAA")
        ingest("ts2", "task.set", 50, task="BBB")
        ingest("under-b", "prompt.begin", 60, title="under-b", task="BBB")

        assert task_of("before") == "AAA"
        assert task_of("before-2") == "AAA"
        assert task_of("under-a") == "AAA"
        assert task_of("under-b") == "BBB"

        # A late, older untagged event arrives after the previous task.set, so the
        # next one backfills only its own range and leaves this one alone.
        ingest("late", "prompt.begin", 25, title="late")
        ingest("fresh", "prompt.begin", 65, title="fresh")
        ingest("other", "prompt.begin", 25, title="other", program="p2")
        ingest("ts3", "task.set", 70, task="CCC")

        assert task_of("late") is None
        assert task_of("fresh") == "CCC"
        assert task_of("other") is None


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


def test_git_actor_override_keeps_author(tmp_path: Path) -> None:
    with make_client(tmp_path, GIT_SOURCE) as client:
        commit = {
            "kind": "commit",
            "repository": "/repo",
            "commit": "abc123",
            "message": "hi",
            "author": "Jane",
            "actor": "sildr",
            "timestamp": "2026-01-01T00:00:00Z",
        }

        assert client.post("/api/ingest/git", json=commit).json() == {"accepted": 1}

        event = client.get("/api/tasks", params={"kind": "commit"}).json()[0]
        assert event["actor"] == "sildr"
        assert event["data"]["author"] == "Jane"


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


def test_program_end_closes_at_last_activity(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post("/api/programs/start", json={"program_id": "p1", "source": "opencode", "host": "h1"})

        # The process was last seen two hours ago (e.g. it slept, then exited late).
        last_seen = datetime.now(UTC) - timedelta(hours=2)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        conn = sqlite3.connect(tmp_path / "activity.db")
        conn.execute("update program set last_seen_at = ? where program_id = 'p1'", (fmt(last_seen),))
        conn.commit()
        conn.close()

        assert client.post("/api/programs/end", json={"program_id": "p1"}).status_code == 200

        conn = sqlite3.connect(tmp_path / "activity.db")
        stored_seen, ended = conn.execute(
            "select last_seen_at, ended_at from program where program_id = 'p1'"
        ).fetchone()
        conn.close()

        assert ended is not None
        assert as_utc(datetime.fromisoformat(ended)) == as_utc(last_seen)
        assert as_utc(datetime.fromisoformat(ended)) == as_utc(datetime.fromisoformat(stored_seen))


def test_consolidate_removes_empty_trims_and_splits_idle(tmp_path: Path) -> None:
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

        def event(kind: str, at: datetime) -> None:
            client.post(
                "/api/ingest/opencode",
                json={
                    "id": f"p1-{kind}-{at.isoformat()}",
                    "kind": kind,
                    "actor": "user",
                    "timestamp": fmt(at),
                    "program": {"id": "p1", "source": "opencode", "host": "h1"},
                },
            )

        program("empty", t0, t0 + timedelta(hours=1))
        program("p1", t0, t0 + timedelta(hours=3))

        event("prompt.begin", t0 + timedelta(minutes=10))
        event("prompt.end", t0 + timedelta(minutes=20))
        event("prompt.begin", t0 + timedelta(hours=2, minutes=10))
        event("prompt.end", t0 + timedelta(hours=2, minutes=20))

        result = client.post("/api/programs/consolidate", params={"idle_seconds": 3600}).json()
        assert result == {"removed": 1, "trimmed": 1, "split": 1}

        conn = sqlite3.connect(db_path)
        rows = conn.execute("select program_id, started_at, ended_at from program order by started_at").fetchall()
        conn.close()

        def span(row: tuple[str, str, str]) -> tuple[str, datetime, datetime]:
            return row[0], as_utc(datetime.fromisoformat(row[1])), as_utc(datetime.fromisoformat(row[2]))

        assert [span(row) for row in rows] == [
            ("p1", t0 + timedelta(minutes=10), t0 + timedelta(minutes=20)),
            ("p1", t0 + timedelta(hours=2, minutes=10), t0 + timedelta(hours=2, minutes=20)),
        ]

        # Both split blocks keep their own events on one lane, with the dead gap closed.
        lane = client.get(
            "/api/timelines/presence", params={"start": fmt(t0), "end": fmt(t0 + timedelta(hours=3))}
        ).json()["lanes"][0]
        assert [segment["state"] for segment in lane["segments"]] == ["closed", "active", "closed", "active", "closed"]
        assert len(lane["events"]) == 4

        # Consolidating again is a no-op (idempotent).
        assert client.post("/api/programs/consolidate", params={"idle_seconds": 3600}).json() == {
            "removed": 0,
            "trimmed": 0,
            "split": 0,
        }


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


def test_presence_permission_waiting_returns_to_active(tmp_path: Path) -> None:
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

        def event(pid: str, kind: str, minutes: int, data: dict | None = None) -> None:
            client.post(
                "/api/ingest/opencode",
                json={
                    "id": pid,
                    "kind": kind,
                    "actor": "user",
                    "timestamp": fmt(t0 + timedelta(minutes=minutes)),
                    "data": data or {"sessionID": "sess-1"},
                    "program": {"id": "p1", "source": "opencode", "host": "h1"},
                },
            )

        event("a", "prompt.begin", 5)
        event("b", "permission.asked", 10, {"sessionID": "sess-1", "requestID": "req-1", "action": "bash"})
        event("c", "permission.replied", 12, {"sessionID": "sess-1", "requestID": "req-1", "status": "approved"})
        event("d", "prompt.end", 20)

        lane = client.get(
            "/api/timelines/presence", params={"start": fmt(t0), "end": fmt(t0 + timedelta(hours=1))}
        ).json()["lanes"][0]

        assert [seg["state"] for seg in lane["segments"]] == ["idle", "active", "waiting", "active", "idle"]

        waiting = lane["segments"][2]
        assert datetime.fromisoformat(waiting["start"]) == t0 + timedelta(minutes=10)
        assert datetime.fromisoformat(waiting["end"]) == t0 + timedelta(minutes=12)


def test_presence_question_waiting_returns_to_active(tmp_path: Path) -> None:
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

        def event(pid: str, kind: str, minutes: int, data: dict | None = None) -> None:
            client.post(
                "/api/ingest/opencode",
                json={
                    "id": pid,
                    "kind": kind,
                    "actor": "user",
                    "timestamp": fmt(t0 + timedelta(minutes=minutes)),
                    "data": data or {"sessionID": "sess-1"},
                    "program": {"id": "p1", "source": "opencode", "host": "h1"},
                },
            )

        event("a", "prompt.begin", 5)
        event("b", "question.asked", 9, {"sessionID": "sess-1", "requestID": "frm-1", "header": "Pick"})
        event("c", "question.replied", 14, {"sessionID": "sess-1", "requestID": "frm-1", "status": "answered"})
        event("d", "prompt.end", 20)

        lane = client.get(
            "/api/timelines/presence", params={"start": fmt(t0), "end": fmt(t0 + timedelta(hours=1))}
        ).json()["lanes"][0]

        assert [seg["state"] for seg in lane["segments"]] == ["idle", "active", "waiting", "active", "idle"]

        waiting = lane["segments"][2]
        assert datetime.fromisoformat(waiting["start"]) == t0 + timedelta(minutes=9)
        assert datetime.fromisoformat(waiting["end"]) == t0 + timedelta(minutes=14)


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


def test_user_timeline_open_span_clipped_when_watcher_stops(tmp_path: Path) -> None:
    with make_client(tmp_path, WINDOWS_SOURCE) as client:
        t0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        client.post("/api/ingest/windows", json={"id": "1", "kind": "focus", "task": "code", "timestamp": fmt(t0)})

        body = client.get(
            "/api/timelines/user", params={"start": fmt(t0), "end": fmt(t0 + timedelta(minutes=20))}
        ).json()

        span = body["lanes"][0]["spans"][0]
        assert datetime.fromisoformat(span["start"]) == t0
        # The watcher stopped reporting, so the open span ends one staleness
        # window after the last event instead of stretching to the query end.
        assert datetime.fromisoformat(span["end"]) == t0 + timedelta(seconds=300)


def test_user_timeline_open_span_stays_live_with_recent_events(tmp_path: Path) -> None:
    with make_client(tmp_path, WINDOWS_SOURCE) as client:
        t0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
        fmt = lambda dt: dt.isoformat(" ", timespec="microseconds")

        def send(payload: dict) -> None:
            assert client.post("/api/ingest/windows", json=payload).status_code == 200

        # The watcher keeps pinging, so the open span is still trusted.
        send({"id": "1", "kind": "focus", "task": "code", "timestamp": fmt(t0)})
        send({"id": "2", "kind": "heartbeat", "timestamp": fmt(t0 + timedelta(minutes=19))})

        end = t0 + timedelta(minutes=20)
        body = client.get("/api/timelines/user", params={"start": fmt(t0), "end": fmt(end)}).json()

        spans = body["lanes"][0]["spans"]
        assert len(spans) == 1
        assert datetime.fromisoformat(spans[0]["start"]) == t0
        # The watcher was seen just before the query end, so the open span runs
        # all the way to it.
        assert datetime.fromisoformat(spans[0]["end"]) == end


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


AUTH = ("alice", "s3cret")


def test_basic_auth_disabled_by_default(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        assert client.get("/api/stats").status_code == 200
        assert client.post("/api/ingest/opencode", json={"id": "1", "title": "x"}).status_code == 200


def test_basic_auth_challenges_unauthenticated_requests(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE, basic_auth=AUTH) as client:
        response = client.get("/api/stats")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Basic"


def test_basic_auth_rejects_wrong_credentials(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE, basic_auth=AUTH) as client:
        assert client.get("/api/stats", auth=("alice", "wrong")).status_code == 401
        assert client.get("/api/stats", auth=("bob", "s3cret")).status_code == 401


def test_basic_auth_accepts_valid_credentials(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE, basic_auth=AUTH) as client:
        assert client.get("/api/stats", auth=AUTH).status_code == 200

        event = {"id": "1", "kind": "tool_call", "actor": "agent-a", "title": "edited"}
        assert client.post("/api/ingest/opencode", json=event).status_code == 401
        assert client.post("/api/ingest/opencode", json=event, auth=AUTH).json() == {"accepted": 1}
        assert client.get("/api/stats", auth=AUTH).json()["events"] == 1


def test_basic_auth_rejects_malformed_header(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE, basic_auth=AUTH) as client:
        response = client.get("/api/stats", headers={"Authorization": "Bearer token"})

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Basic"


def test_basic_auth_leaves_docs_public(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE, basic_auth=AUTH) as client:
        assert client.get("/openapi.json").status_code == 200


def test_ingest_records_origin_host(tmp_path: Path) -> None:
    with make_client(tmp_path, PUSH_SOURCE) as client:
        event = {"id": "1", "host": "laptop", "title": "x"}
        assert client.post("/api/ingest/opencode", json=event).json() == {"accepted": 1}

        stored = client.get("/api/tasks").json()[0]
        assert stored["host"] == "laptop"
        assert "host" not in stored["data"]["raw"]


def test_ingest_host_defaults_to_hostname(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACTIVITY_MONITOR_ORIGIN", raising=False)

    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post("/api/ingest/opencode", json={"id": "1", "title": "x"})
        assert client.get("/api/tasks").json()[0]["host"] == socket.gethostname()


def test_origin_env_overrides_ingest_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVITY_MONITOR_ORIGIN", "laptop")

    with make_client(tmp_path, PUSH_SOURCE) as client:
        client.post("/api/ingest/opencode", json={"id": "1", "title": "x"})
        assert client.get("/api/tasks").json()[0]["host"] == "laptop"


def test_host_column_backfilled_for_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "activity.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "create table activityevent ("
        "id integer primary key, source varchar, kind varchar, actor varchar, task varchar,"
        "timestamp datetime, duration_ms integer, title varchar, detail varchar,"
        "tags json, data json, dedup_key varchar)"
    )
    conn.commit()
    conn.close()

    with make_client(tmp_path, PUSH_SOURCE) as client:
        event = {"id": "1", "host": "laptop", "title": "x"}
        assert client.post("/api/ingest/opencode", json=event).json() == {"accepted": 1}
        assert client.get("/api/tasks").json()[0]["host"] == "laptop"

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("pragma table_info(activityevent)")}
    conn.close()
    assert "host" in columns
