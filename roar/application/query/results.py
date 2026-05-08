"""Typed result DTOs for local query workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .diff_graph import JobMatch, JobNode


@dataclass(frozen=True)
class StatusArtifactSummary:
    artifact_hash: str
    size_bytes: int
    path: str
    present: bool


@dataclass(frozen=True)
class StatusSummary:
    dag_hash: str
    build_steps: int
    run_steps: int
    artifacts: list[StatusArtifactSummary] = field(default_factory=list)


@dataclass(frozen=True)
class LineageArtifactSummary:
    hash: str
    path: str
    size: int


@dataclass(frozen=True)
class LineageJobSummary:
    job_uid: str
    step_number: int | None
    command: str
    timestamp: float | int
    duration_seconds: float | int | None
    exit_code: int | None
    inputs: list[dict[str, object]] = field(default_factory=list)
    outputs: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class LineageSummary:
    artifact: LineageArtifactSummary
    jobs: list[LineageJobSummary] = field(default_factory=list)
    artifacts: list[LineageArtifactSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Convert the summary into the stable JSON payload expected by the CLI."""
        return {
            "artifact": asdict(self.artifact),
            "jobs": [asdict(job) for job in self.jobs],
            "artifacts": [asdict(artifact) for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class LogJobSummary:
    job_uid: str | None
    step_number: int | None
    job_type: str | None
    timestamp: float | int
    duration_seconds: float | int | None
    exit_code: int | None
    command: str | None


@dataclass(frozen=True)
class LogSummary:
    jobs: list[LogJobSummary] = field(default_factory=list)


@dataclass(frozen=True)
class LabelEntrySummary:
    key: str
    display_value: str

    def render(self, *, indent: str = "") -> str:
        return f"{indent}{self.key}={self.display_value}"


@dataclass(frozen=True)
class LabelCurrentSummary:
    heading: str | None = None
    entries: list[LabelEntrySummary] = field(default_factory=list)
    empty_message: str = "No labels."

    def render(self) -> str:
        lines: list[str] = []
        if self.heading:
            lines.append(self.heading)
        if not self.entries:
            message = self.empty_message
            if self.heading:
                message = f"  {message}"
            lines.append(message)
            return "\n".join(lines)
        indent = "  " if self.heading else ""
        lines.extend(entry.render(indent=indent) for entry in self.entries)
        return "\n".join(lines)


@dataclass(frozen=True)
class LabelHistoryVersionSummary:
    version: int
    entries: list[LabelEntrySummary] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Version {self.version}:"]
        lines.extend(entry.render(indent="  ") for entry in self.entries)
        return "\n".join(lines)


@dataclass(frozen=True)
class LabelHistorySummary:
    versions: list[LabelHistoryVersionSummary] = field(default_factory=list)

    def render(self) -> str:
        if not self.versions:
            return "No labels."
        return "\n\n".join(version.render() for version in self.versions)


@dataclass(frozen=True)
class ShowHashSummary:
    algorithm: str
    digest: str


@dataclass(frozen=True)
class ShowSessionJobSummary:
    step_number: int | None
    job_uid: str | None
    exit_code: int | None
    command: str | None
    job_type: str | None

    def to_renderer_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShowSessionSummary:
    hash: str
    created_at: float | int
    git_repo: str | None = None
    git_commit_start: str | None = None
    labels: dict[str, Any] | None = None
    jobs: list[ShowSessionJobSummary] = field(default_factory=list)

    def to_renderer_args(
        self,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        session = {
            "hash": self.hash,
            "created_at": self.created_at,
            "git_repo": self.git_repo,
            "git_commit_start": self.git_commit_start,
        }
        jobs = [job.to_renderer_dict() for job in self.jobs]
        return session, jobs, self.labels


@dataclass(frozen=True)
class ShowJobArtifactSummary:
    path: str
    artifact_id: str
    size: int
    kind: str | None = None
    component_count: int | None = None
    hashes: list[ShowHashSummary] = field(default_factory=list)

    def to_renderer_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShowJobSummary:
    job_uid: str
    timestamp: float | int
    duration_seconds: float | int | None
    exit_code: int | None
    command: str | None
    step_number: int | None = None
    job_type: str | None = None
    step_name: str | None = None
    step_identity: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    metadata: dict[str, Any] | None = None
    telemetry: dict[str, Any] | None = None
    labels: dict[str, Any] | None = None
    inputs: list[ShowJobArtifactSummary] = field(default_factory=list)
    outputs: list[ShowJobArtifactSummary] = field(default_factory=list)

    def to_renderer_args(
        self,
    ) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any] | None,
    ]:
        job = {
            "job_uid": self.job_uid,
            "step_number": self.step_number,
            "job_type": self.job_type,
            "step_name": self.step_name,
            "step_identity": self.step_identity,
            "timestamp": self.timestamp,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "command": self.command,
            "git_commit": self.git_commit,
            "git_branch": self.git_branch,
            "metadata": self.metadata,
            "telemetry": self.telemetry,
        }
        inputs = [artifact.to_renderer_dict() for artifact in self.inputs]
        outputs = [artifact.to_renderer_dict() for artifact in self.outputs]
        return job, inputs, outputs, self.labels


@dataclass(frozen=True)
class ShowArtifactLocationSummary:
    path: str

    def to_renderer_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShowArtifactJobSummary:
    job_uid: str | None
    command: str | None
    session_hash: str | None = None  # for the TUI "in this session" marker

    def to_renderer_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShowArtifactComponentSummary:
    relative_path: str | None
    component_digest: str | None
    leaf_kind: str | None

    def to_renderer_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShowArtifactSummary:
    id: str
    size: int
    first_seen_at: float | int
    source: str | None = None
    kind: str | None = None
    component_count: int | None = None
    first_seen_path: str | None = None
    labels: dict[str, Any] | None = None
    composite_summary: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    hashes: list[ShowHashSummary] = field(default_factory=list)
    locations: list[ShowArtifactLocationSummary] = field(default_factory=list)
    produced_by: list[ShowArtifactJobSummary] = field(default_factory=list)
    consumed_by: list[ShowArtifactJobSummary] = field(default_factory=list)
    components: list[ShowArtifactComponentSummary] = field(default_factory=list)

    def to_renderer_args(
        self,
    ) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        dict[str, list[dict[str, Any]]],
        dict[str, Any] | None,
        dict[str, Any] | None,
        list[dict[str, Any]] | None,
    ]:
        artifact = {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "component_count": self.component_count,
            "size": self.size,
            "first_seen_at": self.first_seen_at,
            "first_seen_path": self.first_seen_path,
            "metadata": self.metadata,
            "hashes": [asdict(hash_summary) for hash_summary in self.hashes],
        }
        locations = [location.to_renderer_dict() for location in self.locations]
        jobs = {
            "produced_by": [job.to_renderer_dict() for job in self.produced_by],
            "consumed_by": [job.to_renderer_dict() for job in self.consumed_by],
        }
        components = [component.to_renderer_dict() for component in self.components] or None
        return artifact, locations, jobs, self.labels, self.composite_summary, components


ShowSummary = ShowSessionSummary | ShowJobSummary | ShowArtifactSummary


@dataclass(frozen=True)
class InputArtifactSummary:
    artifact_id: str
    path: str
    size: int
    hashes: list[ShowHashSummary] = field(default_factory=list)


@dataclass(frozen=True)
class InputsSummary:
    target_ref: str
    is_root: bool
    artifacts: list[InputArtifactSummary] = field(default_factory=list)
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target_ref,
            "is_root": self.is_root,
            "inputs": [asdict(a) for a in self.artifacts],
            **({"message": self.message} if self.message else {}),
        }


class ChangeType(str, Enum):
    CONTENT_CHANGED = "content_changed"
    PARAM_CHANGED = "param_changed"
    CODE_CHANGED = "code_changed"
    ENV_CHANGED = "env_changed"
    ADDED = "added"
    REMOVED = "removed"


class DiffCategory(str, Enum):
    DATA = "data"
    CODE = "code"
    PARAMS = "params"
    COMPUTE = "compute"
    PIPELINE = "pipeline"


@dataclass(frozen=True)
class AtomicDiff:
    change_type: ChangeType
    category: DiffCategory
    description: str
    detail: dict[str, Any] = field(default_factory=dict)
    impact: float = 0.0
    is_root_cause: bool = False


@dataclass(frozen=True)
class DiffResult:
    ref_a: str
    ref_b: str
    target_a_hash: str | None
    target_b_hash: str | None
    target_a_path: str | None
    target_b_path: str | None
    targets_identical: bool
    diffs: list[AtomicDiff]
    matched_jobs: list[JobMatch] = field(default_factory=list)
    only_in_a: list[JobNode] = field(default_factory=list)
    only_in_b: list[JobNode] = field(default_factory=list)
    root_cause: AtomicDiff | None = None
