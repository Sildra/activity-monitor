"""Tests for the self-materializing configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from activity_monitor.services.config import AppConfig


def test_missing_file_is_written_with_full_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"

    config = AppConfig.load(path)

    assert path.is_file()
    assert config.general.api_port == 8001
    assert config.general.watcher is False
    assert config.watcher.tray is False
    # The written file loads back to exactly the same config.
    assert AppConfig.load(path) == config


def test_missing_keys_are_filled_in_and_values_kept(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("general:\n  api_port: 9100\nwatcher:\n  tray: true\n", encoding="utf-8")

    config = AppConfig.load(path)

    assert config.general.api_port == 9100  # the user's value survived
    assert config.general.database == "activity.db"  # the missing key got its default
    assert config.watcher.tray is True
    assert config.watcher.poll == 1.0
    # The rewrite healed the file: a second load changes nothing.
    assert AppConfig.load(path) == config


def test_complete_file_is_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    AppConfig.load(path)
    written = path.read_text(encoding="utf-8")

    AppConfig.load(path)

    assert path.read_text(encoding="utf-8") == written


def test_non_mapping_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("just a string", encoding="utf-8")

    with pytest.raises(TypeError):
        AppConfig.load(path)
