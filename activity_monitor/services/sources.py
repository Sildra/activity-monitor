"""Source providers.

A source is anything that produces activity events; every producer is an
external client that posts events to the ingest endpoint. A provider only has
to implement `parse(raw) -> dict`, turning a source-native record into the
canonical event fields — the storage layer is agnostic to where the record
came from. Names without a built-in provider get a transient `PushSource`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from activity_monitor.util.time import parse_timestamp


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

USER_SOURCE = "windows"
"""Source name of the desktop watcher, shared by its events and the timelines over them."""


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


_STAT_LIMIT = 8000
"""Stored size cap for a `git show --stat` block (a commit touching hundreds of files)."""

_STAT_FILE = re.compile(r"^\s*(.+?)\s+\|\s+(?:Bin |[0-9 +-]+$)")
_STAT_CHANGED = re.compile(r"^\s*(\d+) files? changed")
_STAT_INSERTIONS = re.compile(r"(\d+) insertions?\(\+\)")
_STAT_DELETIONS = re.compile(r"(\d+) deletions?\(\-\)")

_REWRITE_KINDS = ("rebase", "amend")
"""History-rewriting operations reported by the `post-rewrite` git hook."""


class StatSummary(NamedTuple):
    """What one commit's `git show --stat` block says."""

    block: str  # the block itself, capped
    files: list[str]  # the paths it lists, in order
    changed: int | None
    insertions: int | None
    deletions: int | None

    def compact(self) -> str:
        """A one-line form (`2 files, +10/-6`) for the event's detail text."""
        changed = self.changed if self.changed is not None else len(self.files)

        if not changed and self.insertions is None and self.deletions is None:
            return ""

        return f"{changed} {'file' if changed == 1 else 'files'}, +{self.insertions or 0}/-{self.deletions or 0}"


def parse_stat(block: str) -> StatSummary:
    """Read a `git show --stat` block: per-file lines, then `N files changed, ...`.

    A file line is `path | 12 +++----` (or `path | Bin 123 -> 456 bytes`); a
    rename keeps git's `old => new` spelling. Only the summary line carries the
    counts, so a block without it keeps them all `None`.
    """
    text = block[:_STAT_LIMIT]
    files: list[str] = []
    changed = insertions = deletions = None

    for line in text.splitlines():
        summary = _STAT_CHANGED.match(line)

        if summary:
            changed = int(summary.group(1))
            found = _STAT_INSERTIONS.search(line)
            insertions = int(found.group(1)) if found else None
            found = _STAT_DELETIONS.search(line)
            deletions = int(found.group(1)) if found else None
            continue

        file_line = _STAT_FILE.match(line)

        if file_line:
            files.append(file_line.group(1))

    return StatSummary(block=text.strip(), files=files, changed=changed, insertions=insertions, deletions=deletions)


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
    def __init__(self, name: str, settings: dict[str, Any]):
        self.name = name
        self.settings = settings
        # Latest liveness ping (`heartbeat` record) seen from this source. Pings
        # are not persisted as events (see `services.events.store_events`); they only tell
        # the user timeline that a watcher is still reporting.
        self.last_heartbeat: datetime | None = None

    @property
    def actor(self) -> str:
        return str(self.settings.get("actor") or "user")

    @abstractmethod
    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Map a source-native record to the canonical event fields."""


class PushSource(SourceProvider):
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


class GitHistorySource(SourceProvider):
    """The git hooks' source: parses `post-commit`/`pre-push`/`post-rewrite` payloads."""

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        repository = str(raw.get("repository", ""))
        commit = str(raw.get("commit", ""))
        message = str(raw.get("message", "")).strip()
        branch = str(raw.get("branch") or "").strip()
        kind = str(raw.get("kind") or "commit").strip() or "commit"
        remote = str(raw.get("remote") or "").strip()
        reference = str(raw.get("reference") or "").strip()
        label = branch or reference or commit
        stat = str(raw.get("stat") or "").strip()
        stats = parse_stat(stat) if stat else None

        if kind == "commit":
            title = message[:120]
            # The diff summary rides along in the detail text (still starting
            # with the subject, so a search by message keeps working).
            summary = stats.compact() if stats is not None else ""
            detail = f"{message} · {summary}" if summary else message
            dedup_key = dedup(self.name, repository, commit)
        elif kind in _REWRITE_KINDS:
            rewritten = raw.get("rewritten") if isinstance(raw.get("rewritten"), dict) else {}
            count = int(rewritten.get("count") or 0)
            # `rebase <branch>` / `amend <branch>`, with how many commits moved.
            title = f"{kind} {label}"
            detail = f"{title} · {count} {'commit' if count == 1 else 'commits'}" if count else title
            dedup_key = dedup(self.name, kind, repository, commit, str(raw.get("previous") or ""))
        else:
            title = f"{kind} {label} → {remote}".strip()
            detail = f"{title} ({commit[:12]})"
            dedup_key = dedup(self.name, kind, repository, reference or label, commit, remote)

        data: dict[str, Any] = {
            "source": self.name,
            "repository": repository,
            "commit": commit,
            "branch": branch or None,
            "reference": reference or None,
            "remote": remote or None,
            "previous": raw.get("previous"),
            "author": raw.get("author"),
            "email": raw.get("email"),
        }

        if stats is not None:
            data["stat"] = stats.block or None
            data["files"] = stats.files or None
            data["insertions"] = stats.insertions
            data["deletions"] = stats.deletions

        if kind in _REWRITE_KINDS:
            # The old -> new map ties the rewritten commits to the ones the
            # rebase replaced; `onto` is the commit they were replayed onto.
            data["rewritten"] = rewritten or None
            data["onto"] = raw.get("onto") or None

        if raw.get("replayed"):
            # post-commit flags the commits a rebase replayed (new ids, so they
            # are new events; this is what tells them from fresh work).
            data["replayed"] = True

        return make_event(
            kind=kind,
            timestamp=parse_timestamp(raw.get("timestamp")),
            actor=str(raw.get("actor") or raw.get("author") or self.actor),
            task=derive_task(message, branch) or (Path(repository).name or None),
            title=title,
            detail=detail,
            tags=[tag for tag in ("git", kind, branch) if tag],
            data=data,
            dedup_key=dedup_key,
        )


BUILTIN_SOURCES: dict[str, type[SourceProvider]] = {
    # The desktop watcher's source: always registered so its heartbeats have a
    # home and the user timeline can tell a live watcher from a stopped one.
    USER_SOURCE: PushSource,
    # The git hooks' source: registered so their payloads parse through
    # GitHistorySource (kind branching, stat summary, task derivation).
    "git": GitHistorySource,
}


def build_sources() -> dict[str, SourceProvider]:
    """The built-in providers that exist regardless of configuration.

    Every other name gets a transient `PushSource` on ingest; only these two
    need a stable identity (heartbeats, parsing).
    """
    return {name: provider_cls(name, {}) for name, provider_cls in BUILTIN_SOURCES.items()}
