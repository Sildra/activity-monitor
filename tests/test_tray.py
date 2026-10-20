"""Tests for the standalone watcher's tray icon (ctypes-only Win32)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only tray icon")


def load_tray_module() -> Any:
    from activity_monitor.util import tray

    return tray


def test_make_disc_icon_returns_handle() -> None:
    tray = load_tray_module()
    icon = tray.make_disc_icon((46, 160, 67))

    try:
        assert icon
    finally:
        tray.user32.DestroyIcon(icon)


def test_tray_icon_tracks_state_and_stops() -> None:
    tray = load_tray_module()
    icon = tray.TrayIcon("Activity Monitor — test")

    try:
        icon.start()
        icon.set_state(None)
        icon.set_state(True)
        icon.set_state(True)  # a repeat is ignored
        icon.set_state(False)

        assert icon._state == tray.STATE_DISCONNECTED
    finally:
        icon.stop()

    assert not icon._thread.is_alive()
    assert icon._hwnd is None

    icon.stop()  # stopping twice is safe


def test_tray_icon_dedupes_state_changes() -> None:
    tray = load_tray_module()
    icon = tray.TrayIcon("Activity Monitor — test")

    try:
        icon.start()
        icon.set_state(True)
        posts = icon._state
        icon.set_state(True)

        assert icon._state == posts
    finally:
        icon.stop()
