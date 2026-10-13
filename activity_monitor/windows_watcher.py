"""Windows user-activity watcher: reports foreground focus, idle and lock/unlock.

A lightweight, dependency-free (`ctypes` only) push watcher. It samples the
current foreground window, input-idle time and desktop lock state, and reports
transition events so the server has a canonical, deduplicated record of where
attention went.

Events reported (source must be configured as a Push source, e.g. name `windows`):

- `focus`      - foreground window became active (open span); each change closes
                 the previous span carrying its `duration_ms`.
- `idle.start` - no keyboard/mouse input for `idle_minutes`; timestamp is the
                 actual moment input stopped.
- `idle.end`   - input resumed; `duration_ms` is the idle span length.
- `lock`       - workstation locked (input desktop left the interactive one),
                 or the machine was found to have been suspended (see below).
- `unlock`     - workstation unlocked; `duration_ms` is the lock span length.

The machine may also be suspended (sleep/hibernate) while the watcher is not
running, which leaves the wall clock ahead of the time the watcher actually
experienced. Each tick compares the wall clock against the unbiased
interrupt-time count (which stops during sleep); when they diverge by more than
`skew_seconds` the watcher enters the locked state at the last pre-sleep tick and
lets the normal lock handling close it: at wake if the session woke unlocked, or
at the real unlock if the machine locked itself as it slept. Either way the
suspended span is reported as a lock rather than as attention.

Events are idempotent: each carries a unique `id` so re-sends (retries, watcher
restarts) dedupe on the server.

Two ways to run it:

- **Standalone**: `python -m activity_monitor.windows_watcher [--base-url ...]`
  POSTs each event to the running backend's ingest endpoint.
- **Embedded**: the backend starts it in-process (see `--watch-windows` /
  `general.watch_windows`) and events are written straight to the database.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib import request

if TYPE_CHECKING:
    from activity_monitor.sources import SourceProvider

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_SWITCHDESKTOP = 0x0100
UOI_NAME = 2

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwTime", wt.DWORD)]


user32.GetForegroundWindow.restype = wt.HWND
user32.GetForegroundWindow.argtypes = []
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetLastInputInfo.restype = wt.BOOL
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.OpenInputDesktop.restype = wt.HANDLE
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
user32.GetUserObjectInformationW.restype = wt.BOOL
user32.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)]

kernel32.GetTickCount.restype = wt.DWORD
kernel32.GetTickCount.argtypes = []
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryUnbiasedInterruptTime.restype = wt.BOOL
kernel32.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]

EventSink = Callable[[dict[str, Any]], bool]

DEFAULT_PORT = "8001"
DEFAULT_SKEW_SECONDS = 30.0


def default_base_url() -> str:
    """Backend root for the standalone watcher."""
    url = os.environ.get("ACTIVITY_MONITOR_URL") or "http://127.0.0.1:8001"
    return url.rstrip("/")


def local_host() -> str:
    """Origin host for events: ``ACTIVITY_MONITOR_ORIGIN`` or the OS hostname."""
    return os.environ.get("ACTIVITY_MONITOR_ORIGIN") or socket.gethostname()


def build_auth_header() -> str | None:
    """HTTP Basic `Authorization` header from `ACTIVITY_MONITOR_USERNAME`/`_PASSWORD`."""
    username = os.environ.get("ACTIVITY_MONITOR_USERNAME")

    if not username:
        return None

    password = os.environ.get("ACTIVITY_MONITOR_PASSWORD") or ""
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")

    return f"Basic {token}"


def build_ssl_context() -> ssl.SSLContext:
    """TLS context for the standalone watcher (private CA + client certificates).

    - `ACTIVITY_MONITOR_CA_CERT`      - CA bundle used to verify the server.
    - `ACTIVITY_MONITOR_CLIENT_CERT`  - client certificate (mTLS).
    - `ACTIVITY_MONITOR_CLIENT_KEY`   - client private key (mTLS).
    - `ACTIVITY_MONITOR_SSL_INSECURE` - `1|true|yes|on` disables verification (dev only).
    """
    cafile = os.environ.get("ACTIVITY_MONITOR_CA_CERT") or None
    certfile = os.environ.get("ACTIVITY_MONITOR_CLIENT_CERT") or None
    keyfile = os.environ.get("ACTIVITY_MONITOR_CLIENT_KEY") or None
    insecure = os.environ.get("ACTIVITY_MONITOR_SSL_INSECURE") or os.environ.get("ACTIVITY_MONITOR_INSECURE") or ""
    context = ssl.create_default_context(cafile=cafile)

    if insecure.lower() in ("1", "true", "yes", "on"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    if certfile:
        context.load_cert_chain(certfile, keyfile)

    return context


def idle_millis() -> int:
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))

    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0

    return (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF


def unbiased_time() -> float | None:
    """Working-state time in seconds since boot, excluding sleep/hibernation.

    The wall clock keeps moving while the machine is suspended, but the unbiased
    interrupt-time count does not, so the difference between the two exposes the
    clock skew a sleeping machine introduces. Returns ``None`` if unavailable.
    """
    value = ctypes.c_ulonglong(0)

    if not kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
        return None

    return value.value / 10_000_000.0


def is_locked() -> bool:
    desktop = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)

    if not desktop:
        return False

    try:
        size = wt.DWORD(0)
        user32.GetUserObjectInformationW(desktop, UOI_NAME, None, 0, ctypes.byref(size))
        buf = ctypes.create_unicode_buffer(size.value + 2)
        user32.GetUserObjectInformationW(desktop, UOI_NAME, buf, size.value + 2, ctypes.byref(size))
        name = buf.value.lower()
    finally:
        kernel32.CloseHandle(desktop)

    return "winlogon" in name or name == "secure desktop"


def foreground_window() -> dict[str, Any] | None:
    hwnd = user32.GetForegroundWindow()

    if not hwnd:
        return None

    title_buf = ctypes.create_unicode_buffer(512)
    n = user32.GetWindowTextW(hwnd, title_buf, 512)
    title = title_buf.value[:n].strip()

    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process: str | None = None
    image: str | None = None

    if pid.value:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)

        if handle:
            try:
                image_buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)

                if kernel32.QueryFullProcessImageNameW(handle, 0, image_buf, ctypes.byref(size)):
                    image = image_buf.value[: size.value]
                    process = image.rsplit("\\", 1)[-1]

                    if process.lower().endswith(".exe"):
                        process = process[:-4]
            finally:
                kernel32.CloseHandle(handle)

    return {"hwnd": int(hwnd), "title": title, "process": process, "image": image, "pid": int(pid.value)}


class HttpSink:
    """Standalone mode: POST each event to the backend ingest endpoint."""

    def __init__(self, base_url: str, source: str) -> None:
        self.url = f"{base_url.rstrip('/')}/api/ingest/{source}"
        self._context = build_ssl_context()
        self._auth = build_auth_header()

    def __call__(self, payload: dict[str, Any]) -> bool:
        headers = {"Content-Type": "application/json"}

        if self._auth is not None:
            headers["Authorization"] = self._auth

        for _ in range(3):
            req = request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

            try:
                with request.urlopen(req, timeout=5, context=self._context):
                    return True
            except OSError:
                continue

        return False


class DatabaseSink:
    """Embedded mode: write each event straight to the database on the app loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, provider: SourceProvider) -> None:
        self._loop = loop
        self._provider = provider

    def __call__(self, payload: dict[str, Any]) -> bool:
        future = asyncio.run_coroutine_threadsafe(self._store(payload), self._loop)

        try:
            future.result(timeout=5)
            return True
        except Exception as error:  # noqa: BLE001 - a failed write must not kill the watcher loop
            print(f"[watcher] database write failed: {error}", flush=True)
            return False

    async def _store(self, payload: dict[str, Any]) -> None:
        from activity_monitor import db
        from activity_monitor.api import store_events

        async with db.new_session() as session:
            await store_events(session, self._provider, [payload])


class WindowsWatcher:
    def __init__(
        self,
        sink: EventSink,
        *,
        source: str = "windows",
        actor: str = "user",
        poll: float = 1.0,
        idle_minutes: float = 2.0,
        skew_seconds: float = DEFAULT_SKEW_SECONDS,
        verbose: bool = False,
    ) -> None:
        self._sink = sink
        self.source = source
        self.actor = actor
        self.host = local_host()
        self.poll = poll
        self.idle_threshold = timedelta(minutes=idle_minutes)
        self.skew_threshold = skew_seconds
        self.verbose = verbose

        self._seq = 0
        self._pending: list[dict[str, Any]] = []
        self._boot_ms = int(datetime.now(UTC).timestamp() * 1000)

        self._hwnd: int | None = None
        self._focus: dict[str, Any] | None = None
        self._focus_start: datetime | None = None

        self._was_idle = False
        self._idle_start: datetime | None = None

        self._was_locked = False
        self._lock_start: datetime | None = None

        self._last_wall: datetime | None = None
        self._last_unbiased: float | None = None

    # -- event plumbing ------------------------------------------------------

    def event(self, *, kind: str, timestamp: datetime, **fields: Any) -> None:
        self._seq += 1
        payload: dict[str, Any] = {
            "id": f"{self._boot_ms}-{self._seq}",
            "kind": kind,
            "timestamp": timestamp.isoformat(timespec="milliseconds"),
            "actor": self.actor,
            "host": self.host,
            **fields,
        }
        self._pending.append(payload)
        self.flush()

        if self.verbose:
            print(f"[watcher] {kind}: {fields.get('title') or fields.get('detail') or ''}", flush=True)

    def flush(self) -> None:
        if not self._pending:
            return

        remaining: list[dict[str, Any]] = []

        for index, payload in enumerate(self._pending):
            if self._sink(payload):
                continue

            remaining.extend(self._pending[index:])
            break

        if len(remaining) > 1000:
            dropped = remaining[1000:]
            remaining = remaining[:1000]
            print(f"[watcher] dropped {len(dropped)} events (queue full)", flush=True)

        self._pending = remaining

    # -- sampling ------------------------------------------------------------

    def detect_sleep(self, now: datetime) -> None:
        """Report a suspension gap as a lock when the wall clock outran working time.

        The unbiased interrupt-time count freezes while the machine sleeps, so a
        wall-clock delta that far exceeds it means the machine was suspended for
        that span (no clock skew accrues from mere scheduling delays, since both
        clocks keep running then).
        """
        unbiased = unbiased_time()
        last_wall = self._last_wall
        last_unbiased = self._last_unbiased

        self._last_wall = now
        self._last_unbiased = unbiased

        if unbiased is None or last_unbiased is None or last_wall is None:
            return

        skew = (now - last_wall).total_seconds() - (unbiased - last_unbiased)

        if skew < self.skew_threshold:
            return

        # Already inside a lock that started before the sleep: its unlock already
        # spans the gap, so there is nothing to add.
        if self._was_locked:
            return

        # Treat the suspended span as a lock that began at the last tick before
        # sleep. The regular lock sampling closes it: on this same tick if the
        # session woke unlocked, or whenever the user unlocks if the machine
        # locked to sleep, so a lock that preceded the sleep is never doubled.
        self.enter_lock(last_wall, detail=f"machine suspended ({skew:.0f}s clock skew)")

    def sample_focus(self, now: datetime) -> None:
        current = foreground_window()

        if self._was_locked:
            return

        if current is None and self._hwnd is None:
            return

        if current is None or current["hwnd"] != self._hwnd:
            previous = self._focus

            if previous is not None and self._focus_start is not None:
                self.event(
                    kind="focus",
                    timestamp=now,
                    title=previous["title"],
                    task=previous["process"],
                    detail=previous["image"] or f"pid {previous['pid']}",
                    duration_ms=max(0.0, (now - self._focus_start).total_seconds()) * 1000.0,
                )

            self._hwnd = current["hwnd"] if current else None
            self._focus = current
            self._focus_start = now if current else None

            if current is not None:
                self.event(
                    kind="focus",
                    timestamp=now,
                    title=current["title"],
                    task=current["process"],
                    detail=current["image"] or f"pid {current['pid']}",
                )

    def sample_idle(self, now: datetime) -> None:
        if self._was_locked:
            self._was_idle = False
            self._idle_start = None
            return

        idle = timedelta(milliseconds=idle_millis())
        is_idle = idle >= self.idle_threshold

        if is_idle and not self._was_idle:
            self._was_idle = True
            self._idle_start = now - idle
            self.event(kind="idle.start", timestamp=self._idle_start)

        if not is_idle and self._was_idle:
            duration = (now - self._idle_start).total_seconds() * 1000.0 if self._idle_start else 0.0
            self._was_idle = False
            self._idle_start = None
            self.event(kind="idle.end", timestamp=now, duration_ms=max(0.0, duration))

    def enter_lock(self, now: datetime, detail: str | None = None) -> None:
        """Enter the locked state, closing any open idle/focus span at `now`."""
        self._was_locked = True
        self._lock_start = now

        if self._was_idle and self._idle_start is not None:
            duration = (now - self._idle_start).total_seconds() * 1000.0
            self._was_idle = False
            self._idle_start = None
            self.event(kind="idle.end", timestamp=now, duration_ms=max(0.0, duration))

        if self._focus is not None and self._focus_start is not None:
            duration = (now - self._focus_start).total_seconds() * 1000.0
            self.event(
                kind="focus",
                timestamp=now,
                title=self._focus["title"],
                task=self._focus["process"],
                detail=self._focus["image"] or f"pid {self._focus['pid']}",
                duration_ms=max(0.0, duration),
            )
            self._hwnd = None
            self._focus = None
            self._focus_start = None

        self.event(kind="lock", timestamp=now, **({"detail": detail} if detail else {}))

    def sample_lock(self, now: datetime) -> None:
        locked = is_locked()

        if locked and not self._was_locked:
            self.enter_lock(now)

        if not locked and self._was_locked:
            duration = (now - self._lock_start).total_seconds() * 1000.0 if self._lock_start else 0.0
            self._was_locked = False
            self._lock_start = None
            self.event(kind="unlock", timestamp=now, duration_ms=max(0.0, duration))
            self._hwnd = None
            self._focus = None
            self._focus_start = None

    # -- loop ----------------------------------------------------------------

    def tick(self, now: datetime) -> None:
        if self.verbose:
            print(f"[watcher] tick {now.isoformat(timespec='seconds')}", flush=True)

        self.detect_sleep(now)
        self.sample_lock(now)
        self.sample_focus(now)
        self.sample_idle(now)
        self.flush()

    def run(self, once: bool = False, until: datetime | None = None, stop: threading.Event | None = None) -> None:
        print(f"[watcher] watching '{self.source}' (poll {self.poll}s)", flush=True)

        try:
            while True:
                now = datetime.now(UTC)
                self.tick(now)

                if once or (until is not None and now >= until):
                    break

                if stop is not None:
                    if stop.wait(self.poll):
                        break
                else:
                    time.sleep(self.poll)
        except KeyboardInterrupt:
            pass
        finally:
            self.flush()


def start_database_watcher(
    provider: SourceProvider,
    *,
    loop: asyncio.AbstractEventLoop,
    source: str | None = None,
    actor: str = "user",
    poll: float = 1.0,
    idle_minutes: float = 2.0,
    skew_seconds: float = DEFAULT_SKEW_SECONDS,
    verbose: bool = False,
) -> tuple[threading.Event, threading.Thread]:
    """Start the watcher in-process, writing events straight to the database."""

    watcher = WindowsWatcher(
        DatabaseSink(loop, provider),
        source=source or provider.name,
        actor=actor,
        poll=poll,
        idle_minutes=idle_minutes,
        skew_seconds=skew_seconds,
        verbose=verbose,
    )
    stop = threading.Event()
    thread = threading.Thread(target=watcher.run, kwargs={"stop": stop}, name="windows-watcher", daemon=True)
    thread.start()

    return stop, thread


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"activity monitor backend root (default $ACTIVITY_MONITOR_PORT, currently {default_base_url()})",
    )
    parser.add_argument("--source", default="windows", help="source name to push under (must exist as Push)")
    parser.add_argument("--actor", default="user")
    parser.add_argument("--poll", type=float, default=1.0, help="sample interval in seconds")
    parser.add_argument("--idle-minutes", type=float, default=2.0, help="idle threshold in minutes")
    parser.add_argument(
        "--skew-seconds",
        type=float,
        default=DEFAULT_SKEW_SECONDS,
        help="wall-clock skew above this many seconds is reported as the machine sleeping",
    )
    parser.add_argument("--run", type=float, default=None, help="exit after N seconds (testing)")
    parser.add_argument("--once", action="store_true", help="sample once and exit (testing)")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_url = args.base_url or default_base_url()
    watcher = WindowsWatcher(
        HttpSink(base_url, args.source),
        source=args.source,
        actor=args.actor,
        poll=args.poll,
        idle_minutes=args.idle_minutes,
        skew_seconds=args.skew_seconds,
        verbose=args.verbose,
    )
    print(f"[watcher] pushing '{args.source}' events to {base_url.rstrip('/')}", flush=True)
    until = datetime.now(UTC) + timedelta(seconds=args.run) if args.run is not None else None
    watcher.run(once=args.once, until=until)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
