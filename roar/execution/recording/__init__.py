"""Execution recording mechanics for local and backend-driven runs."""

from .dataset_identifier import DatasetIdentifierInferer
from .job_recording import (
    CompositeOutputMaterializer,
    ExecutionJobRecorder,
    LocalJobRecorder,
    LocalRecordedArtifact,
    ProxyArtifactRegistrar,
    RunCompositeMaterializationConfig,
    StalenessAnalyzer,
    collect_telemetry,
)

__all__ = [
    "CompositeOutputMaterializer",
    "DatasetIdentifierInferer",
    "ExecutionJobRecorder",
    "LocalJobRecorder",
    "LocalRecordedArtifact",
    "ProxyArtifactRegistrar",
    "RunCompositeMaterializationConfig",
    "StalenessAnalyzer",
    "collect_telemetry",
]
