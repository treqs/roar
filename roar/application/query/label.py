"""Application orchestration for local label workflows."""

from __future__ import annotations

from ...db.context import create_database_context
from ...services.labels import LabelService, flatten_label_metadata, parse_label_pairs
from .requests import (
    LabelCopyRequest,
    LabelHistoryRequest,
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
        result = service.set_metadata(resolved, patch)

    heading = (
        f"Updated labels (version {result.version}):"
        if result.changed
        else f"Labels unchanged (version {result.version}):"
    )
    return _build_current_summary(result.metadata, heading=heading)


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


def _build_current_summary(metadata: dict, *, heading: str | None = None) -> LabelCurrentSummary:
    return LabelCurrentSummary(
        heading=heading,
        entries=_build_label_entries(metadata),
    )


def _build_label_entries(metadata: dict) -> list[LabelEntrySummary]:
    return [
        LabelEntrySummary(key=key, display_value=value)
        for key, value in flatten_label_metadata(metadata)
    ]
