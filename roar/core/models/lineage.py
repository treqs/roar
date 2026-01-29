"""
Lineage domain models.

Provides Pydantic models for lineage query results and JSON output.
"""

from __future__ import annotations

from pydantic import Field

from .base import ImmutableModel


class LineageArtifactInfo(ImmutableModel):
    """Artifact information in lineage output."""

    hash: str = Field(description="BLAKE3 hash digest")
    path: str = Field(description="File path")
    size: int = Field(ge=0, description="File size in bytes")


class LineageJobInfo(ImmutableModel):
    """Job information in lineage output."""

    job_uid: str = Field(description="Unique job identifier")
    step_number: int | None = Field(default=None, description="Step number in DAG")
    command: str = Field(description="Command that was executed")
    timestamp: float = Field(description="Unix timestamp when job started")
    duration_seconds: float | None = Field(default=None, description="Job duration in seconds")
    exit_code: int | None = Field(default=None, description="Exit code of the job")
    inputs: list[LineageArtifactInfo] = Field(
        default_factory=list, description="Input artifacts (on-path only)"
    )
    outputs: list[LineageArtifactInfo] = Field(
        default_factory=list, description="Output artifacts (on-path only)"
    )


class LineageResult(ImmutableModel):
    """Complete lineage result for JSON output."""

    artifact: LineageArtifactInfo = Field(description="Target artifact")
    jobs: list[LineageJobInfo] = Field(
        default_factory=list, description="Jobs in lineage, sorted by timestamp"
    )
    artifacts: list[LineageArtifactInfo] = Field(
        default_factory=list, description="All artifacts on the path to target"
    )
