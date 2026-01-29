"""
Pydantic models for roar.

This package provides typed, validated models for all roar data structures.
All models use Pydantic v2 with strict validation.
"""

# Base models
# Core domain models
from .artifact import Artifact, ArtifactHash
from .base import ImmutableModel, RoarBaseModel
from .command import CommandContext, CommandResult

# Configuration models
from .config import (
    AnalyzersConfig,
    CleanupConfig,
    FiltersConfig,
    GlaasConfig,
    HashConfig,
    LoggingConfig,
    OutputConfig,
    RegisterConfig,
    RoarConfig,
)

# DAG visualization models
from .dag import (
    DagArtifactInfo,
    DagNodeInfo,
    DagNodeMetrics,
    DagNodeState,
    DagVisualization,
)

# GLaaS API models
from .glaas import (
    ArtifactDagResponse,
    ArtifactHashRequest,
    ArtifactResponse,
    CheckTagRequest,
    CheckTagResponse,
    CompleteLiveJobRequest,
    CreateDagRequest,
    CreateLiveJobRequest,
    DagResponse,
    IOEntry,
    JobResponse,
    LineageResponse,
    LiveJobResponse,
    RecordTagRequest,
    RegisterArtifactRequest,
    RegisterArtifactsBatchRequest,
    RegisterJobRequest,
    RegisterJobsBatchRequest,
    RegisterSessionRequest,
    SessionResponse,
    UpdateLiveJobRequest,
)
from .job import Job, JobInput, JobOutput

# Lineage models
from .lineage import LineageArtifactInfo, LineageJobInfo, LineageResult

# Provenance models
from .provenance import (
    ContainerInfo,
    FileClassification,
    FilteredFiles,
    GitInfo,
    HardwareInfo,
    PackageInfo,
    ProvenanceContext,
    PythonInjectData,
    RuntimeInfo,
    TracerData,
)

# Run/execution models
from .run import (
    ResolvedStep,
    RunArguments,
    RunContext,
    RunResult,
    TracerResult,
)

# Session models
from .session import Session

# Telemetry models
from .telemetry import TelemetryRunInfo

# VCS models
from .vcs import VCSInfo

__all__ = [
    "AnalyzersConfig",
    "Artifact",
    "ArtifactDagResponse",
    "ArtifactHash",
    "ArtifactHashRequest",
    "ArtifactResponse",
    "CheckTagRequest",
    "CheckTagResponse",
    "CleanupConfig",
    "CommandContext",
    "CommandResult",
    "CompleteLiveJobRequest",
    "ContainerInfo",
    "CreateDagRequest",
    "CreateLiveJobRequest",
    "DagArtifactInfo",
    "DagNodeInfo",
    "DagNodeMetrics",
    "DagNodeState",
    "DagResponse",
    "DagVisualization",
    "FileClassification",
    "FilteredFiles",
    "FiltersConfig",
    "GitInfo",
    "GlaasConfig",
    "HardwareInfo",
    "HashConfig",
    "IOEntry",
    "ImmutableModel",
    "Job",
    "JobInput",
    "JobOutput",
    "JobResponse",
    "LineageArtifactInfo",
    "LineageJobInfo",
    "LineageResponse",
    "LineageResult",
    "LiveJobResponse",
    "LoggingConfig",
    "OutputConfig",
    "PackageInfo",
    "ProvenanceContext",
    "PythonInjectData",
    "RecordTagRequest",
    "RegisterArtifactRequest",
    "RegisterArtifactsBatchRequest",
    "RegisterConfig",
    "RegisterJobRequest",
    "RegisterJobsBatchRequest",
    "RegisterSessionRequest",
    "ResolvedStep",
    "RoarBaseModel",
    "RoarConfig",
    "RunArguments",
    "RunContext",
    "RunResult",
    "RuntimeInfo",
    "Session",
    "SessionResponse",
    "TelemetryRunInfo",
    "TracerData",
    "TracerResult",
    "UpdateLiveJobRequest",
    "VCSInfo",
]
