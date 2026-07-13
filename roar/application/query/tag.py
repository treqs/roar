"""Application orchestration for roar tag workflows."""

from __future__ import annotations

from pathlib import Path

from ...core.label_constants import TAG_NAMESPACE
from ...db.context import create_database_context
from ..tags import BIND_KIND, BindResult, TagService, parse_tag_kv, tag_display_values
from .requests import (
    TagAddRequest,
    TagBindRequest,
    TagHistoryRequest,
    TagRmRequest,
    TagShowRequest,
    TagUnbindRequest,
)
from .results import (
    LabelCurrentSummary,
    LabelEntrySummary,
    LabelHistorySummary,
    LabelHistoryVersionSummary,
    TagBindArtifactSummary,
    TagBindSummary,
)


def tag_add(request: TagAddRequest) -> str:
    """Append a value to a tag set and return a rendered summary."""
    return build_tag_add_summary(request).render()


def build_tag_add_summary(request: TagAddRequest) -> LabelCurrentSummary:
    """Build the typed summary for a tag add operation."""
    kind, value = parse_tag_kv(request.kv)
    with create_database_context(request.roar_dir) as db_ctx:
        svc = TagService(db_ctx, request.cwd)
        resolved = svc.resolve_target(request.target)
        changed = svc.add(resolved, kind, value)
        tags = svc.get_tags(resolved)

    if changed:
        heading = f"Tagged {request.target}: {TAG_NAMESPACE}.{kind} += [{value!r}]"
    else:
        heading = f"No change: {value!r} already present in {TAG_NAMESPACE}.{kind}"

    return LabelCurrentSummary(
        heading=heading,
        entries=_tag_entries(tags),
        empty_message="(no tags)",
    )


def tag_rm(request: TagRmRequest) -> str:
    """Remove a value (or entire kind) from a tag set and return a rendered summary."""
    return build_tag_rm_summary(request).render()


def build_tag_rm_summary(request: TagRmRequest) -> LabelCurrentSummary:
    """Build the typed summary for a tag rm operation."""
    kind, value = _parse_kind_or_kv(request.key_or_kv)
    with create_database_context(request.roar_dir) as db_ctx:
        svc = TagService(db_ctx, request.cwd)
        resolved = svc.resolve_target(request.target)
        changed = svc.remove(resolved, kind, value)
        tags = svc.get_tags(resolved)

    if changed:
        if value is not None:
            heading = f"Removed {value!r} from {TAG_NAMESPACE}.{kind} on {request.target}"
        else:
            heading = f"Removed {TAG_NAMESPACE}.{kind} from {request.target}"
    else:
        if value is not None:
            heading = f"No change: {value!r} not found in {TAG_NAMESPACE}.{kind}"
        else:
            heading = f"No change: {TAG_NAMESPACE}.{kind} not present on {request.target}"

    return LabelCurrentSummary(
        heading=heading,
        entries=_tag_entries(tags),
        empty_message="(no tags remaining)",
    )


def tag_show(request: TagShowRequest) -> str:
    """Show current tags for a target and return a rendered summary."""
    return build_tag_show_summary(request).render()


def build_tag_show_summary(request: TagShowRequest) -> LabelCurrentSummary:
    """Build the typed summary for showing tags."""
    with create_database_context(request.roar_dir) as db_ctx:
        svc = TagService(db_ctx, request.cwd)
        resolved = svc.resolve_target(request.target)
        tags = svc.get_tags(resolved)

    return LabelCurrentSummary(
        heading=f"Tags on {request.target}:",
        entries=_tag_entries(tags),
        empty_message="(no tags)",
    )


def tag_history(request: TagHistoryRequest) -> str:
    """Show label version history for a target (all keys) and return a rendered summary."""
    return build_tag_history_summary(request).render()


def build_tag_history_summary(request: TagHistoryRequest) -> LabelHistorySummary:
    """Build the typed summary for tag history."""
    with create_database_context(request.roar_dir) as db_ctx:
        svc = TagService(db_ctx, request.cwd)
        resolved = svc.resolve_target(request.target)
        history = svc.history(resolved)

    return LabelHistorySummary(
        versions=[
            LabelHistoryVersionSummary(
                version=int(row["version"]),
                entries=_tag_entries(row["metadata"].get(TAG_NAMESPACE) or {}),
            )
            for row in history
            if isinstance(row.get("metadata"), dict)
        ]
    )


def tag_bind(request: TagBindRequest) -> str:
    """Promote each target's current tags to cross-session scope and return a rendered summary."""
    return build_tag_bind_summary(request).render()


def build_tag_bind_summary(request: TagBindRequest) -> TagBindSummary:
    """Build the typed summary for a tag bind operation."""
    return _build_bind_or_unbind_summary(
        request.roar_dir, request.cwd, request.targets, action="bind"
    )


def tag_unbind(request: TagUnbindRequest) -> str:
    """Revoke each target's currently-bound tags and return a rendered summary."""
    return build_tag_unbind_summary(request).render()


def build_tag_unbind_summary(request: TagUnbindRequest) -> TagBindSummary:
    """Build the typed summary for a tag unbind operation."""
    return _build_bind_or_unbind_summary(
        request.roar_dir, request.cwd, request.targets, action="unbind"
    )


def _build_bind_or_unbind_summary(
    roar_dir: Path, cwd: Path, targets: tuple[str, ...], *, action: str
) -> TagBindSummary:
    artifacts: list[TagBindArtifactSummary] = []
    with create_database_context(roar_dir) as db_ctx:
        svc = TagService(db_ctx, cwd)
        for target in targets:
            resolved = svc.resolve_target(target)
            result: BindResult = svc.bind(resolved) if action == "bind" else svc.unbind(resolved)

            size = None
            if resolved.entity_type == "artifact" and resolved.artifact_id:
                artifact = db_ctx.artifacts.get(resolved.artifact_id)
                if artifact:
                    size = artifact.get("size")

            artifacts.append(
                TagBindArtifactSummary(
                    display_target=resolved.display_target or target,
                    action=action,
                    changed=result.changed,
                    promoted=result.promoted,
                    size=size,
                )
            )
    return TagBindSummary(artifacts=artifacts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_kind_or_kv(key_or_kv: str) -> tuple[str, str | None]:
    """Parse ``kind`` or ``kind=value``. Returns (kind, value_or_None)."""
    if "=" in key_or_kv:
        kind, value = parse_tag_kv(key_or_kv)
        return kind, value
    kind = key_or_kv.strip()
    if not kind:
        raise ValueError("Kind cannot be empty.")
    return kind, None


def _tag_entries(tags: dict) -> list[LabelEntrySummary]:
    """Convert a tag.* subtree to display entries (values only — skips the bind ledger)."""
    return [
        LabelEntrySummary(key=kind, display_value=", ".join(tag_display_values(tags[kind])))
        for kind in sorted(tags)
        if kind != BIND_KIND and tag_display_values(tags[kind])
    ]
