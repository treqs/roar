"""
GLaaS API models.

Provides Pydantic models for GLaaS (Graph Lineage-as-a-Service) API requests and responses.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from .base import ImmutableModel, RoarBaseModel

# Type aliases
HashAlgorithm = Literal["blake3", "sha256", "sha512", "md5"]
HexDigest = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[a-f0-9]+$")]
SourceType = Literal["s3", "gs", "https"] | None


# -------------------------------------------------------------------------
# Artifact API Models
# -------------------------------------------------------------------------


class ArtifactHashRequest(ImmutableModel):
    """Hash in artifact registration request."""

    algorithm: HashAlgorithm
    digest: HexDigest

    @field_validator("digest", mode="before")
    @classmethod
    def normalize_digest(cls, v: str) -> str:
        """Normalize digest to lowercase."""
        if isinstance(v, str):
            return v.lower()
        return v


class RegisterArtifactRequest(RoarBaseModel):
    """Request to register an artifact with GLaaS."""

    hashes: Annotated[list[ArtifactHashRequest], Field(min_length=1)]
    size: Annotated[int, Field(ge=0)]
    source_type: SourceType = None
    source_url: str | None = None
    metadata: str | None = None


class RegisterArtifactsBatchRequest(RoarBaseModel):
    """Batch artifact registration request."""

    artifacts: Annotated[list[RegisterArtifactRequest], Field(min_length=1)]


class ArtifactResponse(ImmutableModel):
    """Artifact lookup response."""

    model_config = ImmutableModel.model_config.copy()
    model_config["extra"] = "allow"

    id: str
    hashes: list[dict[str, str]] = Field(default_factory=list)
    size: int
    source_type: str | None = None
    source_url: str | None = None


class LineageResponse(ImmutableModel):
    """Artifact lineage response."""

    model_config = ImmutableModel.model_config.copy()
    model_config["extra"] = "allow"

    artifact: ArtifactResponse
    producer: dict[str, Any] | None = None
    inputs: list[dict[str, Any]] = Field(default_factory=list)


# -------------------------------------------------------------------------
# Job API Models
# -------------------------------------------------------------------------


class RegisterJobRequest(RoarBaseModel):
    """Request to register a job with GLaaS."""

    command: Annotated[str, Field(min_length=1)]
    timestamp: Annotated[float, Field(gt=0)]
    job_uid: str | None = None
    git_repo: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    duration_seconds: Annotated[float, Field(ge=0)] | None = None
    exit_code: int | None = None
    input_hashes: list[str] | None = None
    output_hashes: list[str] | None = None
    metadata: str | None = None
    job_type: str | None = None


class RegisterJobsBatchRequest(RoarBaseModel):
    """Batch job registration request."""

    jobs: Annotated[list[RegisterJobRequest], Field(min_length=1)]


class JobResponse(ImmutableModel):
    """Job response from API."""

    model_config = ImmutableModel.model_config.copy()
    model_config["extra"] = "allow"

    id: int | None = None
    job_uid: str | None = None
    status: str | None = None


# -------------------------------------------------------------------------
# Session API Models
# -------------------------------------------------------------------------


class RegisterSessionRequest(RoarBaseModel):
    """Request to register a session with GLaaS."""

    hash: Annotated[str, Field(min_length=8, max_length=64)]
    git_repo: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None


class SessionResponse(ImmutableModel):
    """Response from session registration."""

    model_config = ImmutableModel.model_config.copy()
    model_config["extra"] = "allow"

    hash: str
    url: str | None = None
    created: bool = False


# -------------------------------------------------------------------------
# Live Job API Models
# -------------------------------------------------------------------------


class CreateLiveJobRequest(RoarBaseModel):
    """Request to create a live (running) job."""

    job_uid: Annotated[str, Field(min_length=6)]
    session_hash: Annotated[str, Field(min_length=8)]
    command: Annotated[str, Field(min_length=1)]
    job_type: str = "run"
    step_number: Annotated[int, Field(ge=1)] | None = None
    step_name: str | None = None
    git_repo: str | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    started_at: float | None = None


class IOEntry(RoarBaseModel):
    """Input/output entry for job updates."""

    model_config = RoarBaseModel.model_config.copy()
    model_config["extra"] = "allow"

    path: Annotated[str, Field(min_length=1)]
    hash: str | None = None
    size: Annotated[int, Field(ge=0)] | None = None


class UpdateLiveJobRequest(RoarBaseModel):
    """Request to update a running job."""

    inputs: list[IOEntry] | None = None
    outputs: list[IOEntry] | None = None
    elapsed_seconds: Annotated[float, Field(ge=0)] | None = None
    telemetry: str | None = None


class CompleteLiveJobRequest(RoarBaseModel):
    """Request to complete a live job."""

    exit_code: int
    duration_seconds: Annotated[float, Field(ge=0)] | None = None
    inputs: list[IOEntry] | None = None
    outputs: list[IOEntry] | None = None
    metadata: str | None = None
    telemetry: str | None = None


class LiveJobResponse(ImmutableModel):
    """Response from live job operations."""

    model_config = ImmutableModel.model_config.copy()
    model_config["extra"] = "allow"

    job_uid: str
    status: str


# -------------------------------------------------------------------------
# DAG API Models
# -------------------------------------------------------------------------


class CreateDagRequest(RoarBaseModel):
    """Request to create a DAG."""

    jobs: list[dict[str, Any]]
    job_ids: list[int]
    metadata: str | None = None


class DagResponse(ImmutableModel):
    """Response from DAG creation."""

    model_config = ImmutableModel.model_config.copy()
    model_config["extra"] = "allow"

    hash: str | None = None
    created: bool = False


class ArtifactDagResponse(ImmutableModel):
    """Response from artifact DAG lookup."""

    model_config = ImmutableModel.model_config.copy()
    model_config["extra"] = "allow"

    artifact: dict[str, Any]
    dag: dict[str, Any] | None = None
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    external_deps: list[dict[str, Any]] = Field(default_factory=list)
    is_external: bool = False


# -------------------------------------------------------------------------
# Tag API Models
# -------------------------------------------------------------------------


class CheckTagRequest(RoarBaseModel):
    """Request to check if a commit is tagged."""

    git_repo: str
    git_commit: str


class CheckTagResponse(ImmutableModel):
    """Response from tag check."""

    tagged: bool
    tag_name: str | None = None


class RecordTagRequest(RoarBaseModel):
    """Request to record a tagged commit."""

    git_repo: str
    git_commit: str
    tag_name: str
