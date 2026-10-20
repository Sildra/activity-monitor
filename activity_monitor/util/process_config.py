r"""Process config: rename, redact and derive tasks from foreground windows.

A small, data-only rule set for the Windows watcher. Each couple matches a
sampled focus window (its process name and window title) and rewrites the
fields the watcher reports:

    process:
      - input:
          name: <regex>
          title: <regex>
        output:
          name: <template>
          title: <template>
          task: <template>

Semantics:

- **input** selects the windows a couple applies to: every specified field is
  searched with ``re.search`` and must match (AND); an omitted field does not
  constrain. Both omitted (``input: {}``) matches every focus event.
- **output** builds the new field values. A template is literal text — that is
  how a program is renamed or redacted — plus ``\g<name>`` backreferences that
  pull from the couple's input captures (named groups of either input regex)
  or the built-ins ``input_name`` / ``input_title`` (the values as this couple
  sees them). That is how a task is extracted from the title or name. An
  unknown or non-participating group expands to an empty string; an empty
  ``task`` means no task (the watcher's fallbacks apply).
- Couples apply **in order**; every matching couple runs, later ones see the
  earlier rewrites (chain them deliberately), and the last ``task`` wins.

This is why the watcher reports the task as configuration-derived (or
``Unknown``) instead of the raw process name: the process name is input data
for these rules, never the task itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_GROUP_REF = re.compile(r"\\g<(\w+)>")
_NUMBERED_REF = re.compile(r"\\[0-9]")
_INPUT_KEYS = frozenset({"name", "title"})
_OUTPUT_KEYS = frozenset({"name", "title", "task"})


class ProcessConfigError(ValueError):
    """The process config file is malformed; raised at load time on purpose."""


@dataclass(frozen=True)
class ProcessRule:
    """One input/output couple: compiled input regexes and output templates."""

    name: re.Pattern[str] | None = None
    title: re.Pattern[str] | None = None
    out_name: str | None = None
    out_title: str | None = None
    out_task: str | None = None


def load_process_config(path: str | Path) -> list[ProcessRule]:
    """Read and validate a process config file (root key ``process``)."""
    file = Path(path)

    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as error:
        raise ProcessConfigError(f"{file}: cannot read: {error}") from error
    except yaml.YAMLError as error:
        raise ProcessConfigError(f"{file}: invalid YAML: {error}") from error

    if data is None:
        raise ProcessConfigError(f"{file}: missing top-level `process` section")

    return parse_process_config(data, source=str(file))


def parse_process_config(data: Any, source: str = "process config") -> list[ProcessRule]:
    """Validate an already-loaded mapping and return its couples in order."""
    if not isinstance(data, Mapping):
        raise ProcessConfigError(f"{source}: root must be a mapping")

    if "process" not in data:
        raise ProcessConfigError(f"{source}: missing top-level `process` section")

    couples = data["process"]

    if couples is None:
        return []

    if isinstance(couples, Mapping):
        couples = [couples]

    if not isinstance(couples, list):
        raise ProcessConfigError(f"{source}: `process` must be a mapping or a list of mappings")

    return [_parse_couple(couple, index, source) for index, couple in enumerate(couples, start=1)]


def _parse_couple(couple: Any, index: int, source: str) -> ProcessRule:
    where = f"{source}: process couple {index}"

    if not isinstance(couple, Mapping):
        raise ProcessConfigError(f"{where} must be a mapping")

    unknown = set(couple) - {"input", "output"}

    if unknown:
        raise ProcessConfigError(f"{where}: unknown keys {sorted(str(key) for key in unknown)} (expected input/output)")

    if "input" not in couple:
        raise ProcessConfigError(f"{where}: missing input (use `input: {{}}` to match everything)")
    if "output" not in couple:
        raise ProcessConfigError(f"{where}: missing output")

    inputs = couple["input"]
    outputs = couple["output"]

    if not isinstance(inputs, Mapping):
        raise ProcessConfigError(f"{where}: input must be a mapping")

    if not isinstance(outputs, Mapping):
        raise ProcessConfigError(f"{where}: output must be a mapping")

    unknown = set(inputs) - _INPUT_KEYS
    if unknown:
        raise ProcessConfigError(
            f"{where}: unknown input keys {sorted(str(key) for key in unknown)} (expected name/title)"
        )

    unknown = set(outputs) - _OUTPUT_KEYS
    if unknown:
        raise ProcessConfigError(
            f"{where}: unknown output keys {sorted(str(key) for key in unknown)} (expected name/title/task)"
        )

    if not any(outputs.get(key) is not None for key in _OUTPUT_KEYS):
        raise ProcessConfigError(f"{where}: output must set name, title or task")

    return ProcessRule(
        name=_compile(inputs.get("name"), f"{where}: input.name"),
        title=_compile(inputs.get("title"), f"{where}: input.title"),
        out_name=_template(outputs.get("name"), f"{where}: output.name"),
        out_title=_template(outputs.get("title"), f"{where}: output.title"),
        out_task=_template(outputs.get("task"), f"{where}: output.task"),
    )


def _compile(pattern: Any, where: str) -> re.Pattern[str] | None:
    if pattern is None:
        return None

    if not isinstance(pattern, str):
        raise ProcessConfigError(f"{where} must be a regex string")

    try:
        return re.compile(pattern)
    except re.error as error:
        raise ProcessConfigError(f"{where}: invalid regex {pattern!r}: {error}") from error


def _template(value: Any, where: str) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise ProcessConfigError(f"{where} must be a string")

    if _NUMBERED_REF.search(value):
        raise ProcessConfigError(f"{where}: numbered backrefs are ambiguous across name/title — use \\g<name>")

    return value


def apply_rules(rules: Sequence[ProcessRule], name: str, title: str) -> tuple[str, str, str | None]:
    """Run the couples in order over one window's name/title.

    Returns the rewritten ``(name, title, task)``; ``task`` is ``None`` when no
    couple produced one (or the last producing couple expanded to empty).
    """
    task: str | None = None

    for rule in rules:
        name_match = rule.name.search(name) if rule.name is not None else None

        if rule.name is not None and name_match is None:
            continue

        title_match = rule.title.search(title) if rule.title is not None else None

        if rule.title is not None and title_match is None:
            continue

        groups: dict[str, str | None] = {"input_name": name, "input_title": title}

        if name_match is not None:
            groups.update(name_match.groupdict())
        if title_match is not None:
            groups.update(title_match.groupdict())

        if rule.out_name is not None:
            name = _expand(rule.out_name, groups)
        if rule.out_title is not None:
            title = _expand(rule.out_title, groups)
        if rule.out_task is not None:
            task = _expand(rule.out_task, groups) or None

    return name, title, task


def _expand(template: str, groups: Mapping[str, str | None]) -> str:
    return _GROUP_REF.sub(lambda match: groups.get(match.group(1)) or "", template)
