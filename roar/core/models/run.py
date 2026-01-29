"""
Run/execution domain models.

Provides Pydantic models for command execution and provenance tracking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, computed_field, field_validator, model_validator

from .base import ImmutableModel, RoarBaseModel

# Type aliases
HashAlgorithm = Literal["blake3", "sha256", "sha512", "md5"]
JobType = Literal["run", "build"]


class RunArguments(ImmutableModel):
    """Parsed arguments for run/build commands."""

    command: Annotated[list[str], Field(min_length=1)]
    quiet: bool = False
    hash_algorithms: list[HashAlgorithm] = Field(default_factory=lambda: ["blake3"])  # type: ignore[arg-type]
    hash_only: bool = False
    dag_reference: Annotated[str, Field(pattern=r"^@B?\d+$")] | None = None
    param_overrides: dict[str, str] = Field(default_factory=dict)
    show_help: bool = False
    is_build: bool = False

    @field_validator("hash_algorithms", mode="before")
    @classmethod
    def validate_algorithms(cls, v: list[str]) -> list[str]:
        """Validate hash algorithms."""
        if not v:
            return ["blake3"]
        valid = {"blake3", "sha256", "sha512", "md5"}
        invalid = set(v) - valid
        if invalid:
            raise ValueError(f"Invalid hash algorithms: {invalid}")
        return v

    @model_validator(mode="after")
    def validate_dag_reference_format(self) -> RunArguments:
        """Validate DAG reference format for build commands."""
        if self.dag_reference and self.is_build and not self.dag_reference.startswith("@B"):
            raise ValueError("Build dag_reference must start with @B")
        return self


class ResolvedStep(ImmutableModel):
    """Result of resolving a DAG reference."""

    step_number: Annotated[int, Field(ge=1)]
    command: Annotated[str, Field(min_length=1)]
    is_build: bool
    original_step: dict[str, Any]
    stale_upstream: list[int] = Field(default_factory=list)


class TracerResult(ImmutableModel):
    """Result of tracer execution."""

    exit_code: int
    duration: Annotated[float, Field(ge=0)]
    tracer_log_path: Annotated[str, Field(min_length=1)]
    inject_log_path: str
    interrupted: bool = False


class RunContext(RoarBaseModel):
    """Context for a run execution."""

    roar_dir: Path
    repo_root: Annotated[str, Field(min_length=1)]
    command: Annotated[list[str], Field(min_length=1)]
    job_type: JobType | None = None
    quiet: bool = False
    hash_algorithms: list[HashAlgorithm] = Field(default_factory=lambda: ["blake3"])  # type: ignore[arg-type]
    git_commit: str | None = None
    git_branch: str | None = None
    git_repo: str | None = None

    @field_validator("roar_dir", mode="before")
    @classmethod
    def ensure_path(cls, v: Any) -> Path:
        """Ensure roar_dir is a Path."""
        return Path(v) if not isinstance(v, Path) else v


class RunResult(ImmutableModel):
    """Complete result of a run execution."""

    exit_code: int
    job_id: int
    job_uid: Annotated[str, Field(min_length=6)]
    duration: Annotated[float, Field(ge=0)]
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    interrupted: bool = False
    is_build: bool = False
    stale_upstream: list[int] = Field(default_factory=list)
    stale_downstream: list[int] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def succeeded(self) -> bool:
        """Check if the run succeeded (exit code 0)."""
        return self.exit_code == 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_stale_dependencies(self) -> bool:
        """Check if there are stale upstream or downstream dependencies."""
        return bool(self.stale_upstream or self.stale_downstream)
