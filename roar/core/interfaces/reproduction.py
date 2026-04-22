"""
Reproduction service interfaces.

Defines protocols for artifact reproduction services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable


@dataclass
class ReproductionResult:
    """Result of a reproduction operation."""

    success: bool
    repo_dir: Path | None = None
    steps_run: int = 0
    steps_total: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class PipelineInfo:
    """Information about a pipeline to reproduce."""

    artifact_hash: str
    git_repo: str | None
    git_commit: str | None
    target_kind: Literal["artifact", "lineage"] = "artifact"
    session_hash: str | None = None
    build_steps: list[dict] = field(default_factory=list)
    run_steps: list[dict] = field(default_factory=list)
    total_steps: int = 0


@dataclass
class PipelineLookupResult:
    """Typed result for pipeline lookup operations."""

    pipeline: PipelineInfo | None = None
    error: str | None = None
    source: str = "none"  # local | remote | none


@dataclass
class EnvironmentInfo:
    """Information about a reproduction environment."""

    repo_dir: Path
    venv_dir: Path | None
    python_version: str | None
    packages: list[str] = field(default_factory=list)


@runtime_checkable
class IEnvironmentSetupService(Protocol):
    """Protocol for environment setup service."""

    def setup(
        self,
        pipeline: PipelineInfo,
        target_dir: Path,
        auto_confirm: bool,
    ) -> EnvironmentInfo:
        """
        Set up reproduction environment.

        Args:
            pipeline: Pipeline information
            target_dir: Directory to set up in
            auto_confirm: Auto-confirm prompts

        Returns:
            EnvironmentInfo with setup details
        """
        ...


@runtime_checkable
class IPipelineExecutor(Protocol):
    """Protocol for pipeline execution service."""

    def execute(
        self,
        pipeline: PipelineInfo,
        environment: EnvironmentInfo,
        auto_confirm: bool,
    ) -> tuple[int, int]:
        """
        Execute pipeline steps.

        Args:
            pipeline: Pipeline to execute
            environment: Execution environment
            auto_confirm: Auto-confirm prompts

        Returns:
            Tuple of (steps_run, steps_total)
        """
        ...
