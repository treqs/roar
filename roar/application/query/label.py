"""Application orchestration for local label workflows."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ...db.context import create_database_context
from ...integrations.glaas import GlaasClient
from ..label_rendering import flatten_label_metadata
from ..labels import LabelService, build_remote_label_mutation_payload, parse_label_pairs
from ..system_labels import strip_reserved_system_labels
from .requests import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelPushRequest,
    LabelSetRequest,
    LabelShowRequest,
)
from .results import (
    LabelCurrentSummary,
    LabelEntrySummary,
    LabelHistorySummary,
    LabelHistoryVersionSummary,
)


def set_labels(request: LabelSetRequest) -> str:
    """Patch the current label document for a target."""
    return build_set_labels_summary(request).render()


def build_set_labels_summary(request: LabelSetRequest) -> LabelCurrentSummary:
    """Build the typed summary for a label set operation."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        resolved = service.resolve_target(request.entity_type, request.target)
        patch = parse_label_pairs(request.pairs)
        current_metadata = service.current_metadata(resolved)
        result = service.set_metadata(resolved, patch)

    heading = (
        f"Updated labels (version {result.version}):"
        if result.changed
        else f"Labels unchanged (version {result.version}):"
    )
    changed_metadata = _extract_changed_metadata(current_metadata, result.metadata)
    return _build_current_summary(
        changed_metadata,
        heading=heading,
        empty_message="No label changes.",
    )


def copy_labels(request: LabelCopyRequest) -> str:
    """Copy the current source label document into the destination as a patch."""
    return build_copy_labels_summary(request).render()


def build_copy_labels_summary(request: LabelCopyRequest) -> LabelCurrentSummary:
    """Build the typed summary for a label copy operation."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        source = service.resolve_target(request.source_entity_type, request.source_target)
        destination = service.resolve_target(
            request.destination_entity_type,
            request.destination_target,
        )
        result = service.copy_metadata(source, destination)

    heading = (
        f"Copied labels (version {result.version}):"
        if result.changed
        else f"Copy made no changes (version {result.version}):"
    )
    return _build_current_summary(result.metadata, heading=heading)


def push_labels(request: LabelPushRequest) -> str:
    """Push the current local user-managed label document for a target to GLaaS."""
    return build_push_labels_summary(request).render()


def build_push_labels_summary(request: LabelPushRequest) -> LabelCurrentSummary:
    """Build the typed summary for a remote label push operation."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        resolved = service.resolve_target(request.entity_type, request.target)
        metadata = strip_reserved_system_labels(service.current_metadata(resolved))
        if not metadata:
            raise ValueError(f"No local user-managed labels to push for {request.target}.")
        payload = build_remote_label_mutation_payload(
            db_ctx,
            roar_dir=request.roar_dir,
            target=resolved,
            metadata=metadata,
        )

    client = GlaasClient(start_dir=str(request.cwd), allow_public_without_binding=True)
    result, error = client.patch_current_label(payload)
    if error and resolved.entity_type == "job" and error.startswith("HTTP 404:"):
        fallback_payload = build_remote_label_mutation_payload(
            db_ctx,
            roar_dir=request.roar_dir,
            target=resolved,
            metadata=metadata,
            prefer_remote_publication_uid=False,
        )
        result, error = client.patch_current_label(fallback_payload)
    if error:
        raise ValueError(f"Remote label push failed: {error}")

    remote_metadata = metadata
    version: int | None = None
    if isinstance(result, dict):
        returned_metadata = result.get("metadata")
        if isinstance(returned_metadata, dict):
            remote_metadata = strip_reserved_system_labels(returned_metadata)
        raw_version = result.get("version")
        if raw_version is not None:
            try:
                version = int(raw_version)
            except (TypeError, ValueError):
                version = None

    heading = (
        f"Pushed remote labels (version {version}):"
        if version is not None
        else "Pushed remote labels:"
    )
    return _build_current_summary(remote_metadata, heading=heading)


def show_labels(request: LabelShowRequest) -> str:
    """Show the current local label document for a target."""
    return build_show_labels_summary(request).render()


def build_show_labels_summary(request: LabelShowRequest) -> LabelCurrentSummary:
    """Build the typed summary for showing current labels."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        resolved = service.resolve_target(request.entity_type, request.target)
        metadata = service.current_metadata(resolved)
    return _build_current_summary(metadata)


def label_history(request: LabelHistoryRequest) -> str:
    """Show all local label versions for a target."""
    return build_label_history_summary(request).render()


def build_label_history_summary(request: LabelHistoryRequest) -> LabelHistorySummary:
    """Build the typed summary for label history."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        resolved = service.resolve_target(request.entity_type, request.target)
        history = service.history(resolved)

    return LabelHistorySummary(
        versions=[
            LabelHistoryVersionSummary(
                version=int(row["version"]),
                entries=_build_label_entries(row["metadata"]),
            )
            for row in history
        ]
    )


def _build_current_summary(
    metadata: dict,
    *,
    heading: str | None = None,
    empty_message: str = "No labels.",
) -> LabelCurrentSummary:
    return LabelCurrentSummary(
        heading=heading,
        entries=_build_label_entries(metadata),
        empty_message=empty_message,
    )


_UNCHANGED = object()


def _extract_changed_metadata(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changed = _diff_metadata(before, after)
    return changed if isinstance(changed, dict) else {}


def _diff_metadata(before: Any, after: Any) -> Any:
    if isinstance(before, dict) and isinstance(after, dict):
        changed: dict[str, Any] = {}
        for key, after_value in after.items():
            if key not in before:
                changed[key] = deepcopy(after_value)
                continue
            diff = _diff_metadata(before[key], after_value)
            if diff is not _UNCHANGED:
                changed[key] = diff
        return changed if changed else _UNCHANGED

    if before == after:
        return _UNCHANGED
    return deepcopy(after)


def _build_label_entries(metadata: dict) -> list[LabelEntrySummary]:
    return [
        LabelEntrySummary(key=key, display_value=value)
        for key, value in flatten_label_metadata(metadata)
    ]
