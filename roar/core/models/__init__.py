"""Lazy exports for core Pydantic model types."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "Artifact": ".artifact",
    "ArtifactDagResponse": ".glaas",
    "ArtifactHash": ".artifact",
    "ArtifactHashRequest": ".glaas",
    "ArtifactResponse": ".glaas",
    "CheckTagRequest": ".glaas",
    "CheckTagResponse": ".glaas",
    "CompleteLiveJobRequest": ".glaas",
    "ContainerInfo": ".provenance",
    "CreateDagRequest": ".glaas",
    "CreateLiveJobRequest": ".glaas",
    "DagArtifactInfo": ".dag",
    "DagNodeInfo": ".dag",
    "DagNodeMetrics": ".dag",
    "DagNodeState": ".dag",
    "DagResponse": ".glaas",
    "DagVisualization": ".dag",
    "DatasetIdentifier": ".dataset_identifier",
    "FileClassification": ".provenance",
    "FilteredFiles": ".provenance",
    "GitInfo": ".provenance",
    "HardwareInfo": ".provenance",
    "IOEntry": ".glaas",
    "ImmutableModel": ".base",
    "Job": ".job",
    "JobInput": ".job",
    "JobOutput": ".job",
    "JobResponse": ".glaas",
    "LineageArtifactInfo": ".lineage",
    "LineageJobInfo": ".lineage",
    "LineageResponse": ".glaas",
    "LineageResult": ".lineage",
    "LiveJobResponse": ".glaas",
    "PackageInfo": ".provenance",
    "ProvenanceContext": ".provenance",
    "PythonInjectData": ".provenance",
    "RecordTagRequest": ".glaas",
    "RegisterArtifactRequest": ".glaas",
    "RegisterArtifactsBatchRequest": ".glaas",
    "RegisterJobRequest": ".glaas",
    "RegisterJobsBatchRequest": ".glaas",
    "RegisterSessionRequest": ".glaas",
    "ResolvedStep": ".run",
    "RoarBaseModel": ".base",
    "RunArguments": ".run",
    "RunContext": ".run",
    "RunResult": ".run",
    "RuntimeInfo": ".provenance",
    "Session": ".session",
    "SessionResponse": ".glaas",
    "TelemetryRunInfo": ".telemetry",
    "TracerData": ".provenance",
    "TracerResult": ".run",
    "UpdateLiveJobRequest": ".glaas",
    "VCSInfo": ".vcs",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
