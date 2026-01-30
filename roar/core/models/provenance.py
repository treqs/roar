"""
Provenance domain models.

Provides Pydantic models for provenance data collection and assembly.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, computed_field, field_validator

from .base import ImmutableModel, RoarBaseModel


class TracerData(RoarBaseModel):
    """Loaded tracer output data.

    Contains file access information captured by the ptrace-based tracer.
    """

    opened_files: list[str] = Field(default_factory=list)
    read_files: list[str] = Field(default_factory=list)
    written_files: list[str] = Field(default_factory=list)
    processes: list[dict[str, Any]] = Field(default_factory=list)
    start_time: Annotated[float, Field(ge=0)] = 0
    end_time: Annotated[float, Field(ge=0)] = 0

    @field_validator("opened_files", "read_files", "written_files", mode="before")
    @classmethod
    def deduplicate_paths(cls, v: list[str]) -> list[str]:
        """Remove duplicate paths while preserving order."""
        if not v:
            return v
        seen: set[str] = set()
        result = []
        for path in v:
            if path not in seen:
                seen.add(path)
                result.append(path)
        return result

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration(self) -> float:
        """Calculate duration from start/end times."""
        return self.end_time - self.start_time if self.end_time > self.start_time else 0


class PythonInjectData(RoarBaseModel):
    """Loaded Python inject output data.

    Contains Python-specific information captured by sitecustomize injection.
    """

    modules_files: list[str] = Field(default_factory=list)
    env_reads: dict[str, str] = Field(default_factory=dict)
    sys_prefix: str = ""
    sys_base_prefix: str = ""
    roar_inject_dir: str = ""
    shared_libs: list[str] = Field(default_factory=list)
    used_packages: dict[str, str | None] = Field(default_factory=dict)
    installed_packages: dict[str, str] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_venv(self) -> bool:
        """Check if running in a virtual environment."""
        return self.sys_prefix != self.sys_base_prefix


class FilteredFiles(ImmutableModel):
    """Result of file filtering.

    Contains file lists after noise filtering has been applied.
    """

    read_files: list[str] = Field(default_factory=list)
    written_files: list[str] = Field(default_factory=list)
    opened_files: list[str] = Field(default_factory=list)
    modules_files: list[str] = Field(default_factory=list)
    tmp_files_deleted: Annotated[int, Field(ge=0)] = 0


class HardwareInfo(RoarBaseModel):
    """Hardware information."""

    model_config = RoarBaseModel.model_config.copy()
    model_config["extra"] = "allow"

    cpu: dict[str, Any] | None = None
    memory: dict[str, int] | None = None
    gpu: list[dict[str, Any]] | None = None
    cuda: dict[str, str] | None = None


class ContainerInfo(RoarBaseModel):
    """Container environment detection."""

    model_config = RoarBaseModel.model_config.copy()
    model_config["extra"] = "allow"

    type: str | None = None
    id: str | None = None
    image: str | None = None


class RuntimeInfo(RoarBaseModel):
    """Runtime environment information.

    Contains comprehensive runtime context including OS, Python, hardware.
    """

    model_config = RoarBaseModel.model_config.copy()
    model_config["extra"] = "allow"

    hostname: str = ""
    timing: dict[str, Any] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    os: dict[str, str] = Field(default_factory=dict)
    python: dict[str, str] = Field(default_factory=dict)
    env_vars: dict[str, str] = Field(default_factory=dict)
    container: dict[str, str] | None = None
    vm: dict[str, str] | None = None
    cuda: dict[str, str] | None = None
    gpu: list[dict[str, Any]] | None = None
    cpu: dict[str, Any] | None = None
    memory: dict[str, int] | None = None

    @field_validator("command", mode="before")
    @classmethod
    def ensure_command_list(cls, v: Any) -> list[str]:
        """Ensure command is a list."""
        if isinstance(v, str):
            return [v]
        return v if v else []


class PackageInfo(RoarBaseModel):
    """Package information by manager."""

    model_config = RoarBaseModel.model_config.copy()
    model_config["extra"] = "allow"

    pip: dict[str, str] = Field(default_factory=dict)
    dpkg: dict[str, str] = Field(default_factory=dict)
    build_dpkg: dict[str, str] = Field(default_factory=dict)
    build_pip: dict[str, str] = Field(default_factory=dict)
    rpm: dict[str, str] = Field(default_factory=dict)
    conda: dict[str, str] = Field(default_factory=dict)


class GitInfo(RoarBaseModel):
    """Git repository information."""

    commit: str | None = None
    branch: str | None = None
    remote_url: str | None = None
    clean: bool = True
    uncommitted_changes: list[str] = Field(default_factory=list)
    commit_timestamp: str | None = None
    commit_message: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def short_commit(self) -> str | None:
        """Get short form of commit hash."""
        return self.commit[:7] if self.commit else None


class FileClassification(ImmutableModel):
    """File classification results."""

    tracked: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    external: list[str] = Field(default_factory=list)
    site_packages: list[str] = Field(default_factory=list)


class ProvenanceContext(RoarBaseModel):
    """Context passed between provenance services.

    Aggregates all provenance data collected during execution.
    """

    repo_root: Annotated[str, Field(min_length=1)]
    tracer_data: TracerData
    python_data: PythonInjectData
    filtered_files: FilteredFiles
    runtime_info: RuntimeInfo
    process_summary: list[dict[str, Any]] = Field(default_factory=list)
    classification: dict[str, Any] = Field(default_factory=dict)
    git_info: dict[str, Any] = Field(default_factory=dict)
    packages: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    analyzer_results: dict[str, Any] | None = None
