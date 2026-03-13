"""Application-owned metadata shaping for publish workflows."""

from __future__ import annotations

import json
from typing import Any

from ...core.operation_metadata import build_operation_metadata_json
from ...services.registration._dataset_label import build_dataset_metadata, find_matching_identifier
from ...services.registration._dataset_profile import build_dataset_profile
from .composite_builder import CompositeBuildResult


def build_publish_composite_dataset_metadata_payload(
    *,
    root_path: str,
    dataset_identifiers: list[dict[str, Any]] | None,
    artifact_metadata: Any = None,
    components: list[dict[str, Any]] | None = None,
    component_count_total: int | None = None,
) -> dict[str, Any] | None:
    """Build normalized dataset metadata for a publish composite."""
    dataset_metadata = _extract_dataset_metadata_from_artifact_metadata(artifact_metadata)
    derived_profile = build_dataset_profile(
        components or [],
        total_components=component_count_total,
    )

    if dataset_metadata is None and dataset_identifiers:
        matching = find_matching_identifier(root_path, dataset_identifiers)
        if matching is not None:
            extracted = build_dataset_metadata(matching)
            if extracted:
                dataset_metadata = extracted

    if dataset_metadata is None and derived_profile is None:
        return None

    payload = dict(dataset_metadata or {})
    if derived_profile is not None:
        payload["profile"] = derived_profile
    return payload


def build_publish_composite_dataset_metadata_json(
    *,
    root_path: str,
    dataset_identifiers: list[dict[str, Any]] | None,
    artifact_metadata: Any = None,
    components: list[dict[str, Any]] | None = None,
    component_count_total: int | None = None,
) -> str | None:
    """Serialize normalized dataset metadata for composite registration."""
    payload = build_publish_composite_dataset_metadata_payload(
        root_path=root_path,
        dataset_identifiers=dataset_identifiers,
        artifact_metadata=artifact_metadata,
        components=components,
        component_count_total=component_count_total,
    )
    if payload is None:
        return None
    return json.dumps({"dataset": payload}, separators=(",", ":"))


def build_local_publish_composite_metadata_json(
    *,
    composite: CompositeBuildResult,
    dataset_identifiers: list[dict[str, Any]] | None = None,
) -> str:
    """Serialize local composite metadata for persistence in roar.db."""
    metadata: dict[str, Any] = {
        "composite": {
            "root_path": composite.root_path,
            "component_count_total": composite.component_count_total,
            "component_count_stored": composite.component_count_stored,
        }
    }

    dataset_payload = build_publish_composite_dataset_metadata_payload(
        root_path=composite.root_path,
        dataset_identifiers=dataset_identifiers,
        components=list(composite.payload.get("components") or []),
        component_count_total=composite.component_count_total,
    )
    if dataset_payload is not None:
        metadata["dataset"] = dataset_payload

    return json.dumps(metadata)


def build_put_operation_metadata_json(
    *,
    message: str,
    destination: str,
    destination_type: str,
    artifact_urls: dict[str, str],
    composite_registrations: list[dict[str, Any]],
    lineage_composite_registrations: list[dict[str, Any]],
    dataset_identifiers: list[dict[str, Any]],
    git_commit: str | None,
    git_tag: str | None,
    timestamp: float,
) -> str:
    """Serialize put job metadata."""
    return build_operation_metadata_json(
        "put",
        {
            "message": message,
            "destination": destination,
            "destination_type": destination_type,
            "artifacts": artifact_urls,
            "composites": composite_registrations,
            "lineage_composites": lineage_composite_registrations,
            "dataset_identifiers": dataset_identifiers,
            "git_commit": git_commit,
            "git_tag": git_tag,
            "timestamp": timestamp,
        },
    )


def _extract_dataset_metadata_from_artifact_metadata(
    artifact_metadata: Any,
) -> dict[str, Any] | None:
    parsed_metadata: dict[str, Any] | None = None

    if isinstance(artifact_metadata, dict):
        parsed_metadata = artifact_metadata
    elif isinstance(artifact_metadata, str):
        try:
            parsed = json.loads(artifact_metadata)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            parsed_metadata = parsed

    if parsed_metadata is None:
        return None

    dataset = parsed_metadata.get("dataset")
    if not isinstance(dataset, dict):
        return None

    normalized = build_dataset_metadata(dataset)
    if not normalized:
        return None
    return normalized
