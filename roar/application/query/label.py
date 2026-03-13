"""Application orchestration for local label workflows."""

from __future__ import annotations

from ...db.context import create_database_context
from ...services.labels import LabelService, parse_label_pairs, render_label_lines
from .requests import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelSetRequest,
    LabelShowRequest,
)


def set_labels(request: LabelSetRequest) -> str:
    """Patch the current label document for a target."""
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
    return _render_current(result.metadata, heading=heading)


def copy_labels(request: LabelCopyRequest) -> str:
    """Copy the current source label document into the destination as a patch."""
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
    return _render_current(result.metadata, heading=heading)


def show_labels(request: LabelShowRequest) -> str:
    """Show the current local label document for a target."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        resolved = service.resolve_target(request.entity_type, request.target)
        metadata = service.current_metadata(resolved)
    return _render_current(metadata)


def label_history(request: LabelHistoryRequest) -> str:
    """Show all local label versions for a target."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        resolved = service.resolve_target(request.entity_type, request.target)
        history = service.history(resolved)

    if not history:
        return "No labels."

    sections: list[str] = []
    for row in history:
        lines = [f"Version {row['version']}:"]
        lines.extend(render_label_lines(row["metadata"], indent="  "))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _render_current(metadata: dict, *, heading: str | None = None) -> str:
    lines: list[str] = []
    if heading:
        lines.append(heading)
    rendered = render_label_lines(metadata, indent="  " if heading else "")
    if not rendered:
        lines.append("No labels.")
    else:
        lines.extend(rendered)
    return "\n".join(lines)
