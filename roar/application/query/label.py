"""Application orchestration for local label workflows."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any

from ...db.context import create_database_context
from ...integrations.glaas import GlaasClient
from ...publish_auth import PublishAuthContext, PublishAuthError, load_publish_auth_context
from ..label_rendering import flatten_label_metadata
from ..labels import (
    LabelService,
    build_reconcile_payload_for_current_lineage,
    build_reconcile_payload_for_target,
    parse_label_pairs,
)
from ..system_labels import strip_reserved_system_labels
from .requests import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelSetRequest,
    LabelShowRequest,
    LabelSyncRequest,
    LabelUnsetRequest,
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


def unset_labels(request: LabelUnsetRequest) -> str:
    """Remove label keys from the current local label document for a target."""
    return build_unset_labels_summary(request).render()


def build_unset_labels_summary(request: LabelUnsetRequest) -> LabelCurrentSummary:
    """Build the typed summary for a label unset operation."""
    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        resolved = service.resolve_target(request.entity_type, request.target)
        current_metadata = service.current_metadata(resolved)
        result = service.unset_metadata(resolved, request.keys)

    heading = (
        f"Removed labels (version {result.version}):"
        if result.changed
        else f"Labels unchanged (version {result.version}):"
    )
    removed_metadata = _extract_removed_metadata(current_metadata, result.metadata)
    return _build_current_summary(
        removed_metadata,
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


def sync_labels(request: LabelSyncRequest) -> str:
    """Sync current local user-managed labels to GLaaS reconcile."""
    summary = build_sync_labels_summary(request)
    if isinstance(summary, str):
        return summary
    return summary.render()


def build_sync_labels_summary(request: LabelSyncRequest) -> LabelCurrentSummary | str:
    """Build the summary for a remote label reconcile operation."""
    if (request.entity_type is None) != (request.target is None):
        raise ValueError("Specify both entity type and target, or neither for current lineage.")

    with create_database_context(request.roar_dir) as db_ctx:
        service = LabelService(db_ctx, request.cwd)
        if request.entity_type and request.target:
            resolved = service.resolve_target(request.entity_type, request.target)
            metadata = strip_reserved_system_labels(service.current_metadata(resolved))
            session_hash, labels, label_ids = build_reconcile_payload_for_target(
                db_ctx,
                roar_dir=request.roar_dir,
                target=resolved,
                metadata=metadata,
            )
            if not labels:
                raise ValueError(
                    f"No local user-managed labels or label deletions to sync for {request.target}."
                )
        else:
            session_hash, labels, label_ids = build_reconcile_payload_for_current_lineage(
                db_ctx,
                roar_dir=request.roar_dir,
            )
            if not labels:
                raise ValueError(
                    "No local user-managed labels or label deletions to sync for current lineage."
                )

    publish_auth = _load_label_sync_publish_auth(request.cwd)

    client = GlaasClient(
        start_dir=str(request.cwd),
        publish_auth=publish_auth,
        allow_public_without_binding=publish_auth.scope_request is None,
    )
    if not client.publish_auth.access_token and not client.publish_auth.ssh_auth_available:
        raise ValueError(
            "Remote label sync requires authentication. Run `roar login` or configure SSH auth."
        )

    payload: dict[str, Any] = {
        "session_hash": session_hash,
        "mode": "sync_user_labels",
        "dry_run": request.dry_run,
        "prune": False,
        "labels": labels,
    }
    if client.publish_auth.scope_request:
        payload["scope"] = dict(client.publish_auth.scope_request)

    result, error = client.reconcile_labels(payload)
    if error:
        if _is_missing_reconcile_route_error(error):
            raise ValueError(
                "Remote label sync requires GLaaS support for /api/v1/labels/reconcile."
            ) from None
        if error.startswith("HTTP 401"):
            raise ValueError(
                "Remote label sync was rejected as unauthenticated. "
                "Run `roar login` (or configure SSH auth) and retry."
            )
        if error.startswith("HTTP 403"):
            raise ValueError(
                f"Remote label sync was denied: {error}. Editing remote labels requires "
                "write access to the lineage's scope (lineage creator, publication "
                "author, project writer, or org admin)."
            )
        raise ValueError(f"Remote label sync failed: {error}")

    if not request.dry_run:
        _mark_labels_synced(request.roar_dir, label_ids)

    if request.output_json:
        return json.dumps(result if isinstance(result, dict) else {}, indent=2, sort_keys=True)

    heading = _render_sync_heading(result, dry_run=request.dry_run)
    return LabelCurrentSummary(
        heading=heading,
        entries=_build_sync_entries(result),
        empty_message="No label changes.",
    )


def show_labels(request: LabelShowRequest) -> str:
    """Show the current local label document for a target."""
    return build_show_labels_summary(request).render()


def _mark_labels_synced(roar_dir: Any, label_ids: list[int]) -> None:
    """Stamp synced_at on pushed label rows; the stamp is the deletion baseline.

    Best-effort: a bookkeeping failure must not fail a sync that already
    succeeded remotely.
    """
    if not label_ids:
        return
    try:
        with create_database_context(roar_dir) as db_ctx:
            mark_synced = getattr(getattr(db_ctx, "labels", None), "mark_synced", None)
            if callable(mark_synced):
                mark_synced(label_ids, time.time())
    except Exception:
        pass


def _is_missing_reconcile_route_error(error: str) -> bool:
    if not error.startswith("HTTP 404:"):
        return False
    normalized = error.lower()
    return "cannot post" in normalized and "/api/v1/labels/reconcile" in normalized


def _load_label_sync_publish_auth(cwd: Any) -> PublishAuthContext:
    """Load sync auth, using repo binding when present and public auth otherwise."""
    try:
        return load_publish_auth_context(
            cwd,
            allow_public_without_binding=False,
        )
    except PublishAuthError as exc:
        message = str(exc)
        if "No GLaaS repo binding found" not in message:
            raise ValueError(message) from exc

    return load_publish_auth_context(
        cwd,
        allow_public_without_binding=True,
    )


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


def _render_sync_heading(result: Any, *, dry_run: bool) -> str:
    payload = result if isinstance(result, dict) else {}
    prefix = "Remote label sync dry run" if dry_run else "Synced remote labels"
    processed = int(payload.get("processed") or 0)
    created = int(payload.get("created") or 0)
    updated = int(payload.get("updated") or 0)
    noops = int(payload.get("noops") or 0)
    conflicts = payload.get("conflicts")
    conflict_count = len(conflicts) if isinstance(conflicts, list) else 0
    deleted_keys = int(payload.get("deletedKeys") or 0)
    suffix = f" processed={processed} created={created} updated={updated} noops={noops}"
    if deleted_keys:
        suffix += f" deleted_keys={deleted_keys}"
    if conflict_count:
        suffix += f" conflicts={conflict_count}"
    return f"{prefix}:{suffix}"


def _build_sync_entries(result: Any) -> list[LabelEntrySummary]:
    if not isinstance(result, dict):
        return []
    rows = result.get("results")
    if not isinstance(rows, list):
        return []

    entries: list[LabelEntrySummary] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entity = str(row.get("entityType") or row.get("entity_type") or "label")
        target = _sync_target_display(row)
        action = str(row.get("action") or "synced")
        version = row.get("version")
        display = action
        if isinstance(version, int):
            display = f"{display} version {version}"
        deleted_keys = row.get("deletedKeys")
        if isinstance(deleted_keys, list) and deleted_keys:
            joined = ", ".join(str(key) for key in deleted_keys)
            display = f"{display} (deleted: {joined})"
        entries.append(LabelEntrySummary(key=f"{entity}:{target}", display_value=display))
    return entries


def _sync_target_display(row: dict[str, Any]) -> str:
    for key in (
        "sessionHash",
        "session_hash",
        "jobUid",
        "job_uid",
        "artifactHash",
        "artifact_hash",
    ):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return "unknown"


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


def _extract_removed_metadata(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    removed = _removed_metadata(before, after)
    return removed if isinstance(removed, dict) else {}


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


def _removed_metadata(before: Any, after: Any) -> Any:
    if isinstance(before, dict) and isinstance(after, dict):
        removed: dict[str, Any] = {}
        for key, before_value in before.items():
            if key not in after:
                removed[key] = deepcopy(before_value)
                continue
            diff = _removed_metadata(before_value, after[key])
            if diff is not _UNCHANGED:
                removed[key] = diff
        return removed if removed else _UNCHANGED

    if before == after:
        return _UNCHANGED
    return _UNCHANGED


def _build_label_entries(metadata: dict) -> list[LabelEntrySummary]:
    return [
        LabelEntrySummary(key=key, display_value=value)
        for key, value in flatten_label_metadata(metadata)
    ]
