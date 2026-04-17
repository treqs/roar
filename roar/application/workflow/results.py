"""Typed result DTOs for TReqs workflow-generation workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GeneratedWorkflowTask:
    """Summary of one generated workflow task."""

    step_ref: str
    task_name: str


@dataclass(frozen=True)
class GenerateWorkflowResult:
    """Application response for a generated TReqs workflow."""

    output_path: Path
    display_path: str
    workflow_name: str
    session_hash: str
    session_id: int
    working_directory: str | None
    tasks: tuple[GeneratedWorkflowTask, ...]
