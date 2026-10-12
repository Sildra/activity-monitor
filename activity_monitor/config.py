"""Plain mapping-based configuration.

Deliberately lightweight: a `config.yaml` (optional) is loaded with `yaml.safe_load`
into a flat `AppConfig`. There is no configuration service, no runtime editing and
no node tree — the database file lives next to the process unless overridden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DATABASE = "activity.db"
DEFAULT_PORT = 8001
DEFAULT_SESSION_GAP_MINUTES = 30
DEFAULT_STALE_AFTER_SECONDS = 300


@dataclass
class AppConfig:
    database: str = DEFAULT_DATABASE
    api_port: int = DEFAULT_PORT
    session_gap_minutes: int = DEFAULT_SESSION_GAP_MINUTES
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    watch_windows: bool = False
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    pipelines: list[dict[str, Any]] = field(default_factory=list)
    scanners: dict[str, dict[str, Any]] = field(default_factory=dict)
    ssl_enabled: bool = False
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    ssl_ca_certs: str | None = None
    ssl_cert_reqs: str = "none"  # none|optional|required

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        data: dict[str, Any] = {}

        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        if not isinstance(data, dict):
            raise TypeError("Configuration root must be a mapping")

        general = data.get("general") or {}
        session = data.get("session") or {}

        return cls(
            database=str(general.get("database", DEFAULT_DATABASE)),
            api_port=int(general.get("api_port", DEFAULT_PORT)),
            session_gap_minutes=int(session.get("gap_minutes", DEFAULT_SESSION_GAP_MINUTES)),
            stale_after_seconds=int(general.get("stale_after_seconds", DEFAULT_STALE_AFTER_SECONDS)),
            watch_windows=bool(general.get("watch_windows", False)),
            ssl_enabled=bool(general.get("ssl_enabled", False)),
            ssl_certfile=general.get("ssl_certfile"),
            ssl_keyfile=general.get("ssl_keyfile"),
            ssl_ca_certs=general.get("ssl_ca_certs"),
            ssl_cert_reqs=str(general.get("ssl_cert_reqs", "none")),
            sources=dict(data.get("sources") or {}),
            pipelines=list(data.get("pipelines") or []),
            scanners=dict(data.get("scanners") or {}),
        )

    def source_settings(self, name: str) -> dict[str, Any]:
        return dict(self.sources.get(name) or {})

    def scanner_settings(self, name: str) -> dict[str, Any]:
        return dict(self.scanners.get(name) or {})
