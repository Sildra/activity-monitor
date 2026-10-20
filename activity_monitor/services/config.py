"""Self-materializing mapping-based configuration.

`config.yaml` is the one file both apps read: the backend
(`activity_monitor.main`) uses the `general` section and embeds the desktop
watcher when `general.watcher` is set; the standalone watcher
(`python -m activity_monitor.services.windows_watcher`) reads only the
`watcher` section and pushes events over HTTP.

The file writes itself: missing on disk it is created with every default, and a
file missing keys is rewritten with the gaps filled in (existing values survive
the rewrite; comments and unknown keys do not). There is no configuration
service and no runtime editing — the database file lives next to the process
unless overridden.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DATABASE = "activity.db"
DEFAULT_API_PORT = 8001
DEFAULT_STALE_AFTER_SECONDS = 300
DEFAULT_SOURCE = "windows"
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_IDLE_MINUTES = 2.0
DEFAULT_SKEW_SECONDS = 30.0
# How often the watcher emits a `heartbeat` liveness event while it runs. The
# server extends an open focus/idle/lock span only while events keep arriving,
# so the ping keeps it live without spamming when the state is unchanged.
DEFAULT_HEARTBEAT_SECONDS = 60.0


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


@dataclass
class GeneralConfig:
    """The backend app: API server, database, tray."""

    api_port: int = DEFAULT_API_PORT
    database: str = DEFAULT_DATABASE
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    watcher: bool = False
    """Run the desktop watcher inside the backend (direct database ingest). The
    standalone watcher is always on — that is its whole point."""
    ssl_enabled: bool = False
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    ssl_ca_certs: str | None = None
    ssl_cert_reqs: str = "none"  # none|optional|required
    basic_auth_username: str | None = None
    basic_auth_password: str | None = None


@dataclass
class WatcherConfig:
    """The desktop watcher — one OS-agnostic section (only Windows is implemented).

    Both ways of running it read this section; `general.watcher` chooses between
    them. The connection keys (`base_url` and the TLS / basic-auth material) only
    matter standalone.
    """

    tray: bool = False
    source: str = DEFAULT_SOURCE
    actor: str | None = None
    host: str | None = None
    task: str | None = None
    process_config: str | None = None
    poll: float = DEFAULT_POLL_SECONDS
    idle_minutes: float = DEFAULT_IDLE_MINUTES
    skew_seconds: float = DEFAULT_SKEW_SECONDS
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    verbose: bool = False
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    insecure: bool = False


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        data: dict[str, Any] = {}

        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

            if not isinstance(loaded, dict):
                raise TypeError("Configuration root must be a mapping")

            data = loaded

        general = _mapping(data.get("general"))
        watcher = _mapping(data.get("watcher"))

        config = cls(
            general=GeneralConfig(
                api_port=int(general.get("api_port", DEFAULT_API_PORT)),
                database=str(general.get("database", DEFAULT_DATABASE)),
                stale_after_seconds=int(general.get("stale_after_seconds", DEFAULT_STALE_AFTER_SECONDS)),
                watcher=bool(general.get("watcher", False)),
                ssl_enabled=bool(general.get("ssl_enabled", False)),
                ssl_certfile=general.get("ssl_certfile"),
                ssl_keyfile=general.get("ssl_keyfile"),
                ssl_ca_certs=general.get("ssl_ca_certs"),
                ssl_cert_reqs=str(general.get("ssl_cert_reqs", "none")),
                basic_auth_username=_optional_str(general.get("basic_auth_username")),
                basic_auth_password=_optional_str(general.get("basic_auth_password")),
            ),
            watcher=WatcherConfig(
                tray=bool(watcher.get("tray", False)),
                source=str(watcher.get("source", DEFAULT_SOURCE)),
                actor=_optional_str(watcher.get("actor")),
                host=_optional_str(watcher.get("host")),
                task=_optional_str(watcher.get("task")),
                process_config=_optional_str(watcher.get("process_config")),
                poll=float(watcher.get("poll", DEFAULT_POLL_SECONDS)),
                idle_minutes=float(watcher.get("idle_minutes", DEFAULT_IDLE_MINUTES)),
                skew_seconds=float(watcher.get("skew_seconds", DEFAULT_SKEW_SECONDS)),
                heartbeat_seconds=float(watcher.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)),
                verbose=bool(watcher.get("verbose", False)),
                base_url=_optional_str(watcher.get("base_url")),
                username=_optional_str(watcher.get("username")),
                password=_optional_str(watcher.get("password")),
                ca_cert=_optional_str(watcher.get("ca_cert")),
                client_cert=_optional_str(watcher.get("client_cert")),
                client_key=_optional_str(watcher.get("client_key")),
                insecure=bool(watcher.get("insecure", False)),
            ),
        )

        # The file writes itself: a missing file gets every default, and a file
        # missing keys is rewritten with them filled in, so `config.yaml` always
        # documents every knob. A complete file is left untouched.
        if not path.exists() or _has_missing_keys(data, config):
            write(path, config)

        return config


def write(path: Path, config: AppConfig) -> None:
    """Write the full config (every section, every key) as yaml."""
    data = {
        "general": {f.name: getattr(config.general, f.name) for f in fields(config.general)},
        "watcher": {f.name: getattr(config.watcher, f.name) for f in fields(config.watcher)},
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _has_missing_keys(data: dict[str, Any], config: AppConfig) -> bool:
    for name in ("general", "watcher"):
        section = _mapping(data.get(name))

        if any(f.name not in section for f in fields(getattr(config, name))):
            return True

    return False
