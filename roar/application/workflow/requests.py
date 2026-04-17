"""Request DTOs for TReqs workflow-generation workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GenerateWorkflowRequest:
    """Application request for generating a TReqs workflow from a local session."""

    roar_dir: Path
    cwd: Path
    output_path: Path | None = None
    session_ref: str | None = None
    workflow_name: str | None = None
    force: bool = False
