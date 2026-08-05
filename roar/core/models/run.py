"""
Run/execution domain models.

Provides Pydantic models for command execution and provenance tracking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, computed_field, field_validator, model_validator

from ..tracer_modes import TracerMode
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
    backend: str | None = None  # "ebpf" | "preload" | "ptrace"


class RunContext(RoarBaseModel):
    """Context for a run execution."""

    roar_dir: Path
    config_start_dir: Path | None = None
    repo_root: Annotated[str, Field(min_length=1)]
    command: Annotated[list[str], Field(min_length=1)]
    execution_backend: Annotated[str, Field(min_length=1)]
    execution_role: Annotated[str, Field(min_length=1)]
    job_type: JobType | None = None
    step_name: str | None = None
    quiet: bool = False
    # Effective output verbosity for this run. Resolved upstream from CLI
    # flags + config. The legacy `quiet` bool above is kept in sync via a
    # validator below; new code should read `verbosity` directly.
    verbosity: Literal["quiet", "normal", "verbose", "debug"] = "normal"
    hash_algorithms: list[HashAlgorithm] = Field(default_factory=lambda: ["blake3"])  # type: ignore[arg-type]
    tracer_mode: TracerMode | None = None
    tracer_fallback: bool | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    git_repo: str | None = None
    block_tags: list[str] = Field(default_factory=list)
    add_tags: list[str] = Field(default_factory=list)
    # `roar run --wandb-to-trackio`: activate the bundled wandb->trackio alias in
    # the traced child (via ROAR_WANDB_TO_TRACKIO). Recorded so reproduce re-emits it.
    wandb_to_trackio: bool = False

    @field_validator("roar_dir", mode="before")
    @classmethod
    def ensure_path(cls, v: Any) -> Path:
        """Ensure roar_dir is a Path."""
        return Path(v) if not isinstance(v, Path) else v

    @field_validator("config_start_dir", mode="before")
    @classmethod
    def ensure_optional_path(cls, v: Any) -> Path | None:
        """Ensure config_start_dir is a Path when provided."""
        if v is None:
            return None
        return Path(v) if not isinstance(v, Path) else v

    @model_validator(mode="after")
    def _sync_quiet_and_verbosity(self) -> RunContext:
        """If a caller passed only the legacy `quiet=True`, treat it as
        `verbosity="quiet"`. New callers should pass verbosity directly."""
        if self.quiet and self.verbosity == "normal":
            object.__setattr__(self, "verbosity", "quiet")
        if self.config_start_dir is None:
            object.__setattr__(self, "config_start_dir", self.roar_dir.parent)
        return self


def resolve_run_config_start_dir(ctx: object) -> Path:
    """Return the directory used to resolve run/build config for a context."""
    value = getattr(ctx, "config_start_dir", None)
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        return Path(value)

    roar_dir = getattr(ctx, "roar_dir", None)
    if isinstance(roar_dir, Path):
        return roar_dir.parent
    if isinstance(roar_dir, str):
        return Path(roar_dir).parent

    repo_root = getattr(ctx, "repo_root", None)
    if isinstance(repo_root, Path):
        return repo_root
    if isinstance(repo_root, str):
        return Path(repo_root)

    return Path.cwd()


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
    # UX metadata for the new run presenter. Optional so callers that build
    # RunResult without these fields (older code paths, error cases) still work.
    backend: str | None = None
    post_duration: float = 0.0
    proxy_active: bool = False
    pip_count: int = 0
    dpkg_count: int = 0
    env_count: int = 0
    git_branch: str | None = None
    git_short_commit: str | None = None
    git_clean: bool = True
    total_hash_bytes: int = 0
    hash_duration: float = 0.0
    dag_jobs: int = 0
    dag_artifacts: int = 0
    dag_depth: int = 0
    # Session step number for this job (1-indexed per session). Optional
    # so older code paths and synthetic RunResult instances stay valid;
    # the run-time presenter uses it to surface `@N` step references in
    # next-step hints when present, falling back to `--job <uid>` form.
    step_number: int | None = None
    # Per-category counts of files dropped by the noise filter. Empty
    # means counts weren't collected (older code paths or pre-feature
    # runs); presenters should treat that as "no info" and skip the line.
    filter_counts: dict[str, int] = Field(default_factory=dict)
    # Per-category list of dropped paths. Only populated when
    # verbosity="debug"; empty otherwise to keep memory bounded.
    dropped_paths: dict[str, list[str]] = Field(default_factory=dict)

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
