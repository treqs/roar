"""Mechanical helpers for registration secret detection and filtering."""

from __future__ import annotations

from ...core.interfaces.lineage import LineageData
from ...core.interfaces.registration import GitContext
from ...filters.omit import OmitFilter, OmitMatch


def detect_lineage_secrets(
    *,
    lineage: LineageData,
    git_context: GitContext,
    omit_filter: OmitFilter | None,
) -> list[str]:
    """Detect potential secrets in lineage without mutating it."""
    if not omit_filter:
        return []

    all_detections: list[OmitMatch] = []

    if git_context.repo:
        all_detections.extend(omit_filter.detect_secrets(git_context.repo, "git_url"))

    for job in lineage.jobs:
        command = job.get("command", "")
        if command:
            all_detections.extend(omit_filter.detect_secrets(command, "command"))

        metadata = job.get("metadata")
        if metadata and isinstance(metadata, str):
            all_detections.extend(omit_filter.detect_secrets(metadata, "metadata"))

    return omit_filter.get_detection_summary(all_detections)


def filter_lineage_secrets(
    *,
    lineage: LineageData,
    omit_filter: OmitFilter | None,
) -> LineageData:
    """Return a filtered lineage copy with sensitive command/metadata content redacted."""
    if not omit_filter:
        return lineage

    filtered_jobs = []
    for job in lineage.jobs:
        filtered_job = dict(job)

        command = filtered_job.get("command", "")
        if command:
            filtered_command, _ = omit_filter.filter_command(command)
            filtered_job["command"] = filtered_command

        metadata = filtered_job.get("metadata")
        if metadata:
            if isinstance(metadata, str):
                filtered_metadata, _ = omit_filter.filter_telemetry(metadata)
                filtered_job["metadata"] = filtered_metadata
            elif isinstance(metadata, dict):
                filtered_metadata_dict, _ = omit_filter.filter_metadata(metadata)
                filtered_job["metadata"] = filtered_metadata_dict  # type: ignore[assignment]

        filtered_jobs.append(filtered_job)

    return LineageData(
        jobs=filtered_jobs,
        artifacts=lineage.artifacts,
        artifact_hashes=lineage.artifact_hashes,
        pipeline=lineage.pipeline,
    )
