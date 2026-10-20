"""Tests for the watcher's process config (rename / redact / task extraction)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from activity_monitor.util.process_config import (
    ProcessConfigError,
    apply_rules,
    load_process_config,
    parse_process_config,
)


def parse(process: Any) -> list[Any]:
    return parse_process_config({"process": process}, source="test")


def test_single_couple_mapping_form() -> None:
    # The documented single-couple shape (no list wrapper).
    rules = parse({"input": {"name": "slack"}, "output": {"name": "[redacted]", "task": "chat"}})

    assert len(rules) == 1
    assert apply_rules(rules, "slack", "standup") == ("[redacted]", "standup", "chat")


def test_missing_process_section_is_an_error() -> None:
    with pytest.raises(ProcessConfigError, match="missing top-level `process`"):
        parse_process_config({"windows": {}}, source="test")


def test_null_process_yields_no_rules() -> None:
    assert parse_process_config({"process": None}, source="test") == []


def test_input_selection_requires_every_specified_field() -> None:
    rules = parse(
        [
            {"input": {"name": "code", "title": "JIRA"}, "output": {"task": "both"}},
            {"input": {"name": "chrome"}, "output": {"task": "browser"}},
        ]
    )

    # Both inputs must match (re.search, so "code" matches "code.exe").
    assert apply_rules(rules, "code.exe", "JIRA-1 board")[2] == "both"
    assert apply_rules(rules, "code", "release notes")[2] is None  # title gate
    assert apply_rules(rules, "chrome", "anything")[2] == "browser"
    assert apply_rules(rules, "slack", "anything")[2] is None

    # Empty input matches everything.
    catch_all = parse({"input": {}, "output": {"name": "any"}})
    assert apply_rules(catch_all, "anything", "at all") == ("any", "at all", None)


def test_task_is_extracted_from_a_title_capture() -> None:
    rules = parse({"input": {"title": r"(?P<task>JIRA-\d+): .*"}, "output": {"task": r"\g<task>"}})

    assert apply_rules(rules, "chrome", "JIRA-42: fix login") == ("chrome", "JIRA-42: fix login", "JIRA-42")
    assert apply_rules(rules, "chrome", "no ticket here")[2] is None


def test_builtins_expose_the_raw_name_and_title() -> None:
    rules = parse(
        {
            "input": {"name": "code"},
            "output": {"title": r"[\g<input_name>] \g<input_title>", "task": r"\g<input_title>"},
        }
    )

    assert apply_rules(rules, "code", "Editor") == ("code", "[code] Editor", "Editor")


def test_couples_chain_in_order() -> None:
    rules = parse(
        [
            {"input": {"name": "code"}, "output": {"name": "IDE"}},
            # Sees the rewritten name from the first couple.
            {"input": {"name": "IDE"}, "output": {"task": "ide", "title": r"\g<input_name> — Editor"}},
        ]
    )

    assert apply_rules(rules, "code", "Editor") == ("IDE", "IDE — Editor", "ide")


def test_unknown_or_unmatched_group_expands_empty() -> None:
    rules = parse({"input": {}, "output": {"task": r"<\g<missing>>"}})

    assert apply_rules(rules, "code", "Editor")[2] == "<>"

    optional = parse({"input": {"title": r"(?:(?P<task>JIRA-\d+))?"}, "output": {"task": r"\g<task>"}})

    # The optional group did not participate -> empty expansion -> no task.
    assert apply_rules(optional, "code", "no ticket")[2] is None


def test_later_empty_task_clears_earlier_one() -> None:
    rules = parse(
        [
            {"input": {"name": "code"}, "output": {"task": "first"}},
            {"input": {"name": "code"}, "output": {"task": r"\g<missing>"}},
        ]
    )

    assert apply_rules(rules, "code", "Editor")[2] is None


@pytest.mark.parametrize(
    ("process", "message"),
    [
        ({"input": {}, "output": {"task": "x"}, "extra": {}}, "unknown keys"),
        ({"input": {"imput": "x"}, "output": {"task": "x"}}, "unknown input keys"),
        ({"input": {}, "output": {"job": "x"}}, "unknown output keys"),
        ({"input": {}, "output": {}}, "output must set"),
        ({"output": {"task": "x"}}, "missing input"),
        ({"input": {}}, "missing output"),
        ({"input": {"name": "["}, "output": {"task": "x"}}, "invalid regex"),
        ({"input": {"name": 5}, "output": {"task": "x"}}, "must be a regex string"),
        ({"input": {}, "output": {"task": ["x"]}}, "must be a string"),
        ({"input": {}, "output": {"task": r"\1"}}, "numbered backrefs"),
        ("nope", "must be a mapping or a list"),
        (["nope"], "must be a mapping"),
    ],
)
def test_malformed_couples_are_rejected(process: Any, message: str) -> None:
    with pytest.raises(ProcessConfigError, match=message):
        parse(process)


def test_error_names_the_couple() -> None:
    with pytest.raises(ProcessConfigError, match="couple 2"):
        parse([{"input": {}, "output": {"task": "ok"}}, {"input": {"name": "["}, "output": {"task": "x"}}])


def test_load_reads_a_file(tmp_path: Path) -> None:
    config = tmp_path / "process.yaml"
    config.write_text(
        "process:\n  - input:\n      title: '(?P<jira>JIRA-\\d+)'\n    output:\n      task: '\\g<jira>'\n",
        encoding="utf-8",
    )

    rules = load_process_config(config)

    assert apply_rules(rules, "chrome", "JIRA-7: bug")[2] == "JIRA-7"


def test_load_rejects_missing_file_and_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(ProcessConfigError, match="cannot read"):
        load_process_config(tmp_path / "absent.yaml")

    broken = tmp_path / "broken.yaml"
    broken.write_text("process: [\n", encoding="utf-8")

    with pytest.raises(ProcessConfigError, match="invalid YAML"):
        load_process_config(broken)

    empty = tmp_path / "empty.yaml"
    empty.write_text("# nothing here\n", encoding="utf-8")

    with pytest.raises(ProcessConfigError, match="missing top-level"):
        load_process_config(empty)
