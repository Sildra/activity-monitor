"""Pluggable event sources.

A source is anything that produces activity events. Two shapes exist:

- **pull** providers (`is_pull = True`): the server asks them for data on demand
  (`fetch`) — e.g. reading a shell history file or walking a git log.
- **push** providers: the source is external and posts events to the ingest
  endpoint; `fetch` yields nothing.

Every provider only has to implement `parse(raw) -> dict`, turning a
source-native record into the canonical event fields. The storage layer is
agnostic to where the record came from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from activity_monitor.config import AppConfig


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


def dedup(*parts: Any) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()


def local_host() -> str:
    """Hostname of the machine that produced an event.

    Overridable with ``ACTIVITY_MONITOR_ORIGIN`` (e.g. ``laptop``); falls back to the
    OS hostname so every event carries where it came from.
    """
    return os.environ.get("ACTIVITY_MONITOR_ORIGIN") or socket.gethostname()


UNKNOWN_TASK = "Unknown"
"""Default task stamped by producers on work they cannot attribute (no task pinned yet)."""


def derive_task(*candidates: str | None) -> str | None:
    """Extract a task key (e.g. a Jira id) from the first candidate that matches.

    The pattern comes from the ``TASK_PATTERN`` environment variable (a regex such as
    ``JIRA-[0-9]+``); the first matching substring wins. Candidates are tried in order
    (git uses the commit message first, then the branch), so commits and branches that
    mention the same key group together under it.
    """
    pattern = os.environ.get("TASK_PATTERN", "").strip()

    if not pattern:
        return None

    try:
        regex = re.compile(pattern)
    except re.error:
        return None

    for candidate in candidates:
        if candidate:
            match = regex.search(candidate)

            if match:
                return match.group(0)

    return None


def make_event(
    *,
    kind: str,
    timestamp: datetime | None,
    actor: str,
    dedup_key: str,
    host: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    task: str | None = None,
    duration_ms: int | None = None,
    tags: list[str] | None = None,
    data: dict | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "timestamp": timestamp or datetime.now(UTC),
        "actor": actor,
        "host": host or local_host(),
        "task": task,
        "title": title,
        "detail": detail,
        "duration_ms": duration_ms,
        "tags": tags or [],
        "data": data or {},
        "dedup_key": dedup_key,
    }


class SourceProvider(ABC):
    KIND = "Base"
    is_pull = False

    def __init__(self, name: str, settings: dict[str, Any]):
        self.name = name
        self.settings = settings
        # Latest liveness ping (`heartbeat` record) seen from this source. Pings
        # are not persisted as events (see `api.store_events`); they only tell
        # the user timeline that a watcher is still reporting.
        self.last_heartbeat: datetime | None = None

    @property
    def actor(self) -> str:
        return str(self.settings.get("actor") or "user")

    def fetch(self) -> list[dict[str, Any]]:
        """Return source-native records to ingest. Pull providers override this."""
        return []

    @abstractmethod
    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map a source-native record to the canonical event fields."""


class PushSource(SourceProvider):
    KIND = "Push"

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        stored = dict(raw)
        timestamp = parse_timestamp(stored.pop("timestamp", None) or stored.pop("time", None))
        kind = str(stored.pop("kind", None) or stored.pop("type", None) or "event")
        actor = str(stored.pop("actor", None) or stored.pop("user", None) or self.actor)
        host = str(stored.pop("host", None) or "")
        task = stored.pop("task", None)
        title = stored.pop("title", None) or stored.pop("summary", None)
        detail = stored.pop("detail", None) or stored.pop("message", None)
        duration_ms = stored.pop("duration_ms", None)
        tags = stored.pop("tags", None)
        external_id = stored.pop("id", None)

        return make_event(
            kind=kind,
            timestamp=timestamp,
            actor=actor,
            host=host,
            task=task,
            title=title,
            detail=detail,
            duration_ms=int(duration_ms) if duration_ms is not None else None,
            tags=list(tags) if isinstance(tags, list) else None,
            data={"source": self.name, "raw": stored},
            dedup_key=dedup(self.name, external_id or json.dumps(raw, sort_keys=True, default=str)),
        )


class BashHistorySource(SourceProvider):
    KIND = "BashHistory"
    is_pull = True

    def history_path(self) -> Path:
        return Path(str(self.settings.get("path") or "~/.bash_history")).expanduser()

    def fetch(self) -> list[dict[str, Any]]:
        path = self.history_path()

        if not path.exists():
            return []

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        limit = int(self.settings.get("max_entries", 1000))
        records: list[dict[str, Any]] = []
        pending: float | None = None

        for index, line in enumerate(lines):
            stripped = line.strip()

            if stripped.startswith("#") and stripped[1:].isdigit():
                pending = float(stripped[1:])
                continue

            if not stripped:
                continue

            records.append({"command": line, "timestamp": pending, "line": index})
            pending = None

        return records[-limit:]

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        command = str(raw.get("command", "")).strip()
        timestamp = parse_timestamp(raw.get("timestamp"))
        mtime = self._mtime()

        return make_event(
            kind="command",
            timestamp=timestamp or mtime,
            actor=self.actor,
            title=command[:120],
            detail=command,
            data={"source": self.name, "command": command},
            dedup_key=dedup(self.name, raw.get("line"), raw.get("timestamp"), command),
        )

    def _mtime(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(self.history_path().stat().st_mtime, tz=UTC)
        except OSError:
            return None


class GitHistorySource(SourceProvider):
    KIND = "GitHistory"
    is_pull = True

    _FIELD = "\x1f"
    _RECORD = "\x1e"

    def repositories(self) -> list[str]:
        configured = self.settings.get("repositories") or []

        return [str(repo) for repo in configured]

    def fetch(self) -> list[dict[str, Any]]:
        limit = int(self.settings.get("max_commits", 500))
        records: list[dict[str, Any]] = []

        for repo in self.repositories():
            if not Path(repo).exists():
                continue

            records.extend(self._fetch_repo(repo, limit))

        return records

    def _fetch_repo(self, repo: str, limit: int) -> list[dict[str, Any]]:
        fmt = self._FIELD.join(("%H", "%an", "%ae", "%aI", "%s")) + self._RECORD

        try:
            result = subprocess.run(
                ["git", "-C", repo, "log", f"--max-count={limit}", f"--pretty=format:{fmt}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []

        if result.returncode != 0:
            return []

        records: list[dict[str, Any]] = []

        for chunk in result.stdout.split(self._RECORD):
            chunk = chunk.strip("\n")

            if not chunk:
                continue

            fields = chunk.split(self._FIELD)

            if len(fields) < 5:
                continue

            commit, author, email, timestamp, message = fields[0], fields[1], fields[2], fields[3], fields[4]
            records.append(
                {
                    "repository": repo,
                    "commit": commit,
                    "author": author,
                    "email": email,
                    "timestamp": timestamp,
                    "message": message,
                }
            )

        return records

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        repository = str(raw.get("repository", ""))
        commit = str(raw.get("commit", ""))
        message = str(raw.get("message", "")).strip()
        branch = str(raw.get("branch") or "").strip()
        kind = str(raw.get("kind") or "commit").strip() or "commit"
        remote = str(raw.get("remote") or "").strip()
        reference = str(raw.get("reference") or "").strip()
        label = branch or reference or commit

        if kind == "commit":
            title = message[:120]
            detail = message
            dedup_key = dedup(self.name, repository, commit)
        else:
            title = f"{kind} {label} → {remote}".strip()
            detail = f"{title} ({commit[:12]})"
            dedup_key = dedup(self.name, kind, repository, reference or label, commit, remote)

        return make_event(
            kind=kind,
            timestamp=parse_timestamp(raw.get("timestamp")),
            actor=str(raw.get("actor") or raw.get("author") or self.actor),
            task=derive_task(message, branch) or (Path(repository).name or None),
            title=title,
            detail=detail,
            tags=[tag for tag in ("git", kind, branch) if tag],
            data={
                "source": self.name,
                "repository": repository,
                "commit": commit,
                "branch": branch or None,
                "reference": reference or None,
                "remote": remote or None,
                "previous": raw.get("previous"),
                "author": raw.get("author"),
                "email": raw.get("email"),
            },
            dedup_key=dedup_key,
        )


PROVIDERS: dict[str, type[SourceProvider]] = {
    provider.KIND: provider for provider in (PushSource, BashHistorySource, GitHistorySource)
}


def build_sources(config: AppConfig) -> dict[str, SourceProvider]:
    sources: dict[str, SourceProvider] = {}

    for name, settings in config.sources.items():
        if not settings.get("enabled", True):
            continue

        kind = str(settings.get("kind") or "Push")
        provider_cls = PROVIDERS.get(kind)

        if provider_cls is None:
            continue

        sources[name] = provider_cls(name, settings)

    return sources
