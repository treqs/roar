"""Typed result DTOs for local query workflows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class StatusArtifactSummary:
    artifact_hash: str
    size_bytes: int
    path: str
    present: bool


@dataclass(frozen=True)
class StatusSummary:
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
