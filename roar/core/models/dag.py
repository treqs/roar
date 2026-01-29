"""
DAG visualization models.

Provides Pydantic models for DAG visualization data structures
used by the `roar dag` command.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .base import ImmutableModel


class DagNodeState(str, Enum):
    """State of a DAG node for visualization."""

    ACTIVE = "active"  # On the shortest valid path
    CACHED = "cached"  # Unchanged, still valid
    STALE = "stale"  # Needs re-execution
    SUPERSEDED = "superseded"  # Old run replaced (expanded view only)


class DagArtifactState(str, Enum):
    """State of a DAG artifact for visualization."""

    ACTIVE = "active"  # Produced by active step, on the execution path
    STALE = "stale"  # Produced by stale step
    SUPERSEDED = "superseded"  # Old version replaced by re-run
    ORPHANED = "orphaned"  # Not consumed by any active step


class DagNodeMetrics(ImmutableModel):
    """Metrics for a DAG node (inputs/outputs/consumed)."""

    inputs: int = Field(ge=0, description="Total number of files read")
    outputs: int = Field(ge=0, description="Number of files written")
    consumed: int = Field(ge=0, description="Files consumed from prior tracked jobs")


class DagNodeInfo(ImmutableModel):
    """Information about a single node in the DAG visualization."""

    step_number: int = Field(ge=1, description="Step number in the session")
    job_id: int = Field(description="Database ID of the job")
    command: str = Field(description="Command that was executed")
    state: DagNodeState = Field(description="Visual state of the node")
    metrics: DagNodeMetrics = Field(description="Input/output/consumed metrics")
    dependencies: list[int] = Field(
        default_factory=list, description="Step numbers this node depends on"
    )
    is_build: bool = Field(default=False, description="Whether this is a build step")
    step_name: str | None = Field(default=None, description="User-assigned step name")
    job_uid: str | None = Field(default=None, description="Unique job identifier")
    exit_code: int | None = Field(default=None, description="Exit code of the job")


class DagArtifactInfo(ImmutableModel):
    """Information about an artifact in the DAG."""

    path: str = Field(description="File path of the artifact")
    hash: str | None = Field(default=None, description="BLAKE3 hash of the artifact")
    is_stale: bool = Field(default=False, description="Whether the artifact is stale")
    producer_step: int | None = Field(
        default=None, description="Step number that produced this artifact"
    )
    state: DagArtifactState = Field(
        default=DagArtifactState.ACTIVE, description="Computed artifact state"
    )
    artifact_id: str | None = Field(default=None, description="UUID for the artifact")
    consumer_steps: list[int] = Field(
        default_factory=list, description="Steps that consume this artifact"
    )
    is_terminal: bool = Field(default=True, description="True if no downstream consumers")
    superseded_by: str | None = Field(default=None, description="Hash of replacing artifact")


class DagVisualization(ImmutableModel):
    """Complete DAG visualization data for rendering."""

    nodes: list[DagNodeInfo] = Field(default_factory=list, description="All nodes in the DAG")
    artifacts: list[DagArtifactInfo] = Field(
        default_factory=list, description="Artifacts in the DAG"
    )
    stale_count: int = Field(ge=0, default=0, description="Number of stale steps")
    total_steps: int = Field(ge=0, default=0, description="Total number of steps")
    is_expanded: bool = Field(
        default=False, description="Whether this shows expanded (all reruns) view"
    )
    session_id: int | None = Field(default=None, description="Session ID")
    stale_artifact_count: int = Field(ge=0, default=0, description="Number of stale artifacts")
    superseded_artifact_count: int = Field(
        ge=0, default=0, description="Number of superseded artifacts"
    )
