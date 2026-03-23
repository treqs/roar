"""OSMO submit planning through the execution backend framework."""

from __future__ import annotations

import os
from pathlib import Path

from roar.backends.osmo.config import load_osmo_backend_config
from roar.execution.framework.contract import ExecutionCommandPlan


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
    if not bool(config.get("force_json_output", True)):
        return list(command)
    if _find_format_type_arg(command) is not None:
        return list(command)
    return [*command, "--format-type", "json"]


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
