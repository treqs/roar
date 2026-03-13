"""Typed result DTOs for artifact reproduction workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ReproducePreviewStepSummary:
    phase: str
    index: int
    command: str


@dataclass(frozen=True)
class ReproduceRequirementBlock:
    label: str
    values: list[str] = field(default_factory=list)
    suffix: str | None = None


@dataclass(frozen=True)
class ReproducePreviewSummary:
    artifact_hash: str
    git_repo: str | None
    git_commit: str | None
    run_hint: str
    build_steps: list[ReproducePreviewStepSummary] = field(default_factory=list)
    run_steps: list[ReproducePreviewStepSummary] = field(default_factory=list)
    requirement_blocks: list[ReproduceRequirementBlock] = field(default_factory=list)


@dataclass(frozen=True)
class ReproduceRunSummary:
    repo_dir: Path | None
    steps_run: int
    steps_total: int
    warnings: list[str] = field(default_factory=list)
