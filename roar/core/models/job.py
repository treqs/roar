"""
Job domain models.

Provides Pydantic models for job executions and their inputs/outputs.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field, computed_field, field_validator

from .base import ImmutableModel, RoarBaseModel

# Type aliases
JobType = Literal["run", "build"]
JobStatus = Literal["pending", "running", "completed", "failed"]


class SupportsJobRow(Protocol):
    """Structural type for ORM-like job rows."""

    id: int
    job_uid: str
    timestamp: float
    command: str
    script: str | None
    step_identity: str | None
    session_id: int | None
    step_number: int | None
    step_name: str | None
    git_repo: str | None
    git_commit: str | None
    git_branch: str | None
    duration_seconds: float | None
    exit_code: int | None
    synced_at: float | None
    status: JobStatus | None
    job_type: JobType | None
    metadata_: str | None
    telemetry: str | None


class JobInput(ImmutableModel):
    """Input artifact reference for a job."""

    artifact_id: Annotated[str, Field(min_length=1)]
    path: Annotated[str, Field(min_length=1)]
    hash: str | None = None
    size: Annotated[int, Field(ge=0)] | None = None
    byte_ranges: list[list[int]] | None = None


class JobOutput(ImmutableModel):
    """Output artifact reference for a job."""

    artifact_id: Annotated[str, Field(min_length=1)]
    path: Annotated[str, Field(min_length=1)]
    hash: str | None = None
    size: Annotated[int, Field(ge=0)] | None = None
    byte_ranges: list[list[int]] | None = None


class Job(RoarBaseModel):
    """Represents a recorded job execution.

    Jobs track command executions with their inputs, outputs, and metadata.
    Each job belongs to a session and may have a step number within a DAG.
    """

    id: int
    job_uid: Annotated[str, Field(min_length=6, max_length=12)]
    timestamp: Annotated[float, Field(gt=0)]
    command: Annotated[str, Field(min_length=1, max_length=65535)]
    script: str | None = None
    step_identity: str | None = None
    session_id: int | None = None
    step_number: Annotated[int, Field(ge=1)] | None = None
    step_name: Annotated[str, Field(max_length=255)] | None = None
    git_repo: str | None = None
    git_commit: Annotated[str, Field(min_length=7, max_length=40)] | None = None
    git_branch: Annotated[str, Field(max_length=255)] | None = None
    duration_seconds: Annotated[float, Field(ge=0)] | None = None
    exit_code: int | None = None
    synced_at: Annotated[float, Field(gt=0)] | None = None
    status: JobStatus | None = None
    job_type: JobType | None = None
    metadata: str | None = None
    telemetry: str | None = None
    inputs: list[JobInput] = Field(default_factory=list)
    outputs: list[JobOutput] = Field(default_factory=list)

    @field_validator("job_uid", mode="before")
    @classmethod
    def normalize_job_uid(cls, v: str) -> str:
        """Normalize job UID to lowercase."""
        if isinstance(v, str):
            return v.lower()
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_build(self) -> bool:
        """Check if this is a build job."""
        return self.job_type == "build"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def succeeded(self) -> bool:
        """Check if the job succeeded (exit code 0)."""
        return self.exit_code == 0

    @classmethod
    def from_orm(
        cls,
        orm_job: SupportsJobRow,
        inputs: list[dict] | None = None,
        outputs: list[dict] | None = None,
    ) -> Job:
        """Create Job from ORM model.

        Args:
            orm_job: SQLAlchemy Job model instance
            inputs: List of input dicts
            outputs: List of output dicts

        Returns:
            Job pydantic model instance
        """
        input_models = []
        if inputs:
            for i in inputs:
                input_models.append(
                    JobInput(
                        artifact_id=i["artifact_id"],
                        path=i["path"],
                        hash=i.get("hash"),
                        size=i.get("size"),
                        byte_ranges=i.get("byte_ranges"),
                    )
                )

        output_models = []
        if outputs:
            for o in outputs:
                output_models.append(
                    JobOutput(
                        artifact_id=o["artifact_id"],
                        path=o["path"],
                        hash=o.get("hash"),
                        size=o.get("size"),
                        byte_ranges=o.get("byte_ranges"),
                    )
                )

        return cls(
            id=orm_job.id,
            job_uid=orm_job.job_uid,
            timestamp=orm_job.timestamp,
            command=orm_job.command,
            script=orm_job.script,
            step_identity=orm_job.step_identity,
            session_id=orm_job.session_id,
            step_number=orm_job.step_number,
            step_name=orm_job.step_name,
            git_repo=orm_job.git_repo,
            git_commit=orm_job.git_commit,
            git_branch=orm_job.git_branch,
            duration_seconds=orm_job.duration_seconds,
            exit_code=orm_job.exit_code,
            synced_at=orm_job.synced_at,
            status=orm_job.status,
            job_type=orm_job.job_type,
            metadata=getattr(orm_job, "metadata_", None),
            telemetry=orm_job.telemetry,
            inputs=input_models,
            outputs=output_models,
        )
