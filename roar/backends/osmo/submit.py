"""OSMO submit planning through the execution backend framework."""

from __future__ import annotations

import os
from pathlib import Path

from roar.backends.osmo.config import load_osmo_backend_config
from roar.execution.framework.contract import ExecutionCommandPlan

_OSMO_MULTIVALUE_OPTIONS = ("--set", "--set-string", "--set-env", "--set-file")


def matches_osmo_workflow_submit_command(command: list[str]) -> bool:
    if len(command) < 3:
        return False
    if not _osmo_backend_enabled():
        return False

    binary = Path(command[0]).name.lower()
    noun = command[1].lower()
    verb = command[2].lower()
    return binary == "osmo" and noun == "workflow" and verb == "submit"


def plan_osmo_workflow_submit_command(command: list[str]) -> ExecutionCommandPlan:
    if not matches_osmo_workflow_submit_command(command):
        return ExecutionCommandPlan(backend_name="osmo", command=list(command))

    normalized_command = _normalize_osmo_submit_command(command)
    return ExecutionCommandPlan(
        backend_name="osmo",
        command=normalized_command,
        execution_role="submit",
    )


def _osmo_backend_enabled() -> bool:
    start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
    return bool(load_osmo_backend_config(start_dir=start_dir).get("enabled", True))


def _normalize_osmo_submit_command(command: list[str]) -> list[str]:
    start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
    config = load_osmo_backend_config(start_dir=start_dir)
    normalized_command = _normalize_multivalue_submit_options(command)
    if not bool(config.get("force_json_output", True)):
        return normalized_command
    if _find_format_type_arg(normalized_command) is not None:
        return normalized_command
    return [*normalized_command, "--format-type", "json"]


def _normalize_multivalue_submit_options(command: list[str]) -> list[str]:
    if len(command) <= 4:
        return list(command)

    grouped_values: dict[str, list[str]] = {option: [] for option in _OSMO_MULTIVALUE_OPTIONS}
    remaining: list[str] = []
    index = 4
    while index < len(command):
        arg = command[index]
        if arg in grouped_values:
            next_index = _collect_multivalue_option_args(
                command,
                start=index + 1,
                values=grouped_values[arg],
            )
            if next_index == index + 1:
                remaining.append(arg)
            index = next_index
            continue

        option, separator, value = arg.partition("=")
        if separator and option in grouped_values:
            if value:
                grouped_values[option].append(value)
            else:
                remaining.append(arg)
            index += 1
            continue

        remaining.append(arg)
        index += 1

    normalized = list(command[:4])
    for option in _OSMO_MULTIVALUE_OPTIONS:
        values = grouped_values[option]
        if values:
            normalized.append(option)
            normalized.extend(values)
    normalized.extend(remaining)
    return normalized


def _collect_multivalue_option_args(
    command: list[str],
    *,
    start: int,
    values: list[str],
) -> int:
    index = start
    while index < len(command) and not command[index].startswith("-"):
        values.append(command[index])
        index += 1
    return index


def _find_format_type_arg(command: list[str]) -> tuple[int, int | None] | None:
    for index, arg in enumerate(command):
        if arg == "--format-type":
            if index + 1 < len(command):
                return index, index + 1
            return index, None
        if arg.startswith("--format-type="):
            return index, None
    return None


__all__ = [
    "matches_osmo_workflow_submit_command",
    "plan_osmo_workflow_submit_command",
]
