"""Application orchestration for local and remote label workflows."""

from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from typing import Any

import click

from ...db.context import create_database_context
from ...integrations.glaas import GlaasClient
from ...publish_auth import PublishAuthContext, PublishAuthError, load_publish_auth_context
from ..label_rendering import flatten_label_metadata
from ..labels import (
    LabelService,
    ReconcileTargetSync,
    build_reconcile_payload_for_current_lineage,
    build_reconcile_payload_for_target,
    parse_label_pairs,
)
from ..system_labels import is_reserved_system_label_path, strip_reserved_system_labels
from .requests import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelSetRequest,
    LabelShowRequest,
    LabelSyncRequest,
    LabelUnsetRequest,
    RemoteLabelHistoryRequest,
    RemoteLabelSetRequest,
    RemoteLabelShowRequest,
    RemoteLabelUnsetRequest,
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
            session_hash, labels, sync_targets = build_reconcile_payload_for_target(
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
            session_hash, labels, sync_targets = build_reconcile_payload_for_current_lineage(
                db_ctx,
                roar_dir=request.roar_dir,
            )
            if not labels:
                raise ValueError(
                    "No local user-managed labels or label deletions to sync for current lineage."
                )

    if not request.dry_run and not request.skip_confirmation:
        _confirm_pending_label_deletions(sync_targets)

    client = _create_remote_label_client(request.cwd, action="sync")

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
        raise ValueError(_map_remote_label_error(error, action="sync")) from None

    if not request.dry_run:
        _mark_labels_synced_confirming_deletions(request.roar_dir, sync_targets, result)

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


def _target_display_name(target: ReconcileTargetSync) -> str:
    """Human-readable identifier for a reconcile target (for prompts/warnings)."""
    if target.entity_type == "job" and target.job_uid:
        return target.job_uid
    if target.entity_type == "artifact" and target.artifact_hash:
        return target.artifact_hash
    if target.session_hash:
        return target.session_hash
    return target.entity_type or "target"


def _confirm_pending_label_deletions(sync_targets: list[ReconcileTargetSync]) -> None:
    """Prompt before a sync that would delete remote label keys.

    Mirrors the preview-then-confirm shape used for anonymous public publishes
    (see ``roar/cli/publish_intent.py::confirm_anonymous_public_publish``),
    reimplemented locally so this module doesn't couple to that one. Aborts
    the process cleanly (no traceback) if the user declines.
    """
    pending = [target for target in sync_targets if target.deleted_keys]
    if not pending:
        return

    click.echo("")
    click.echo("This sync will delete the following remote label keys:")
    for target in pending:
        keys = ", ".join(target.deleted_keys)
        click.echo(f"  {_target_display_name(target)}: {keys}")
    click.echo("Use `roar label sync -y` (or `--yes`) to skip this confirmation in scripts.")
    click.echo("")
    try:
        proceed = click.confirm("Proceed with these remote label deletions?", default=False)
    except click.Abort:
        # click.confirm() raises Abort on EOF (closed/absent stdin) -- the shape a
        # workflow-orchestrated subprocess with no terminal attached actually hits,
        # not a real decline. Without this, that collapses into Click's generic
        # "Aborted!" with no indication why or what to do about it. Deliberately not
        # an up-front sys.stdin.isatty() check: Click's own CliRunner test harness
        # feeds simulated prompt input through a non-tty stream, so isatty() can't
        # tell a real prompt test apart from genuine non-interactive automation --
        # catching the actual EOF click.confirm() raises does.
        raise click.ClickException(
            "Refusing to proceed without confirmation in a non-interactive session "
            "(no input available on stdin). Pass `roar label sync -y` to skip this "
            "confirmation in scripts, CI, or workflow automation."
        ) from None
    if not proceed:
        click.echo("Sync aborted.")
        raise SystemExit(1)


def _reconcile_target_key(
    *,
    entity_type: Any,
    session_hash: Any,
    job_uid: Any,
    artifact_hash: Any,
) -> tuple[str, str, str, str]:
    """Identity key used to correlate an outbound target with a response row."""
    return (
        str(entity_type or ""),
        str(session_hash or ""),
        str(job_uid or ""),
        str(artifact_hash or ""),
    )


def _confirmed_deleted_keys_by_target(result: Any) -> dict[tuple[str, str, str, str], set[str]]:
    """Per-target sets of keys the server's reconcile response confirms deleted."""
    rows = result.get("results") if isinstance(result, dict) else None
    confirmed: dict[tuple[str, str, str, str], set[str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        key = _reconcile_target_key(
            entity_type=row.get("entityType") or row.get("entity_type"),
            session_hash=row.get("sessionHash") or row.get("session_hash"),
            job_uid=row.get("jobUid") or row.get("job_uid"),
            artifact_hash=row.get("artifactHash") or row.get("artifact_hash"),
        )
        row_deleted = row.get("deletedKeys")
        if isinstance(row_deleted, list):
            confirmed.setdefault(key, set()).update(k for k in row_deleted if isinstance(k, str))
    return confirmed


def _mark_labels_synced_confirming_deletions(
    roar_dir: Any,
    sync_targets: list[ReconcileTargetSync],
    result: Any,
) -> None:
    """Advance the local ``synced_at`` baseline for pushed label rows.

    A target whose payload requested key deletions only advances its baseline
    when the server's response confirms *all* of those keys were actually
    deleted. This matters because an old, not-yet-upgraded GLaaS server may
    accept the reconcile POST, silently ignore the unrecognized ``deleted_keys``
    field, and still return HTTP 200 — reading that as unconditional success
    would permanently desync local and remote state (the key never actually
    gets deleted, and roar would never retry because the baseline already
    advanced). Targets with no pending deletions, or with server-confirmed
    deletions, advance normally.
    """
    confirmed_by_key = _confirmed_deleted_keys_by_target(result)

    label_ids_to_mark: list[int] = []
    for target in sync_targets:
        if not target.deleted_keys:
            label_ids_to_mark.extend(target.label_ids)
            continue

        key = _reconcile_target_key(
            entity_type=target.entity_type,
            session_hash=target.session_hash,
            job_uid=target.job_uid,
            artifact_hash=target.artifact_hash,
        )
        confirmed = confirmed_by_key.get(key, set())
        if set(target.deleted_keys).issubset(confirmed):
            label_ids_to_mark.extend(target.label_ids)
        else:
            click.echo(
                f"Warning: sent {len(target.deleted_keys)} label deletion(s) for "
                f"{_target_display_name(target)} but the server did not confirm they were "
                "applied — this GLaaS server may not support remote label deletion yet. "
                "Local state was not marked as synced; retry once the server is upgraded.",
                err=True,
            )

    _mark_labels_synced(roar_dir, label_ids_to_mark)


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


def _is_missing_get_labels_route_error(error: str) -> bool:
    """True when a 404 means the GET route itself is missing (old server).

    Distinguishes this from the app-level "no labels for this target yet" 404
    (a ``NotFoundError`` raised from GLaaS's label service), which must keep
    being treated as "no labels yet". A real glaas-api server's generic
    404 middleware for a genuinely unrecognized route returns a body whose
    message is literally "Endpoint not found"; the app-level case never
    produces that exact string (see roar/integrations/glaas/transport.py's
    ``HTTP {code}: {detail}`` formatting).
    """
    if not error.startswith("HTTP 404:"):
        return False
    return "endpoint not found" in error.lower()


def _missing_get_labels_route_error(path: str) -> str:
    return (
        f"Remote label read requires GLaaS support for GET {path} — "
        "this server may not be upgraded yet."
    )


def _create_remote_label_client(cwd: Any, *, action: str) -> GlaasClient:
    """Build an authenticated GLaaS client for label operations.

    Works from any directory: uses the repo scope binding when present and
    falls back to global auth without a binding otherwise.
    """
    publish_auth = _load_label_sync_publish_auth(cwd)
    client = GlaasClient(
        start_dir=str(cwd),
        publish_auth=publish_auth,
        allow_public_without_binding=publish_auth.scope_request is None,
    )
    if not client.publish_auth.access_token and not client.publish_auth.ssh_auth_available:
        raise ValueError(
            f"Remote label {action} requires authentication. "
            "Run `roar login` or configure SSH auth."
        )
    return client


def _map_remote_label_error(error: str, *, action: str) -> str:
    if _is_missing_reconcile_route_error(error):
        return f"Remote label {action} requires GLaaS support for /api/v1/labels/reconcile."
    if error.startswith("HTTP 401"):
        return (
            f"Remote label {action} was rejected as unauthenticated. "
            "Run `roar login` (or configure SSH auth) and retry."
        )
    if error.startswith("HTTP 403"):
        return (
            f"Remote label {action} was denied: {error}. Editing remote labels requires "
            "write access to the lineage's scope (lineage creator, publication "
            "author, project writer, or org admin)."
        )
    if error.startswith("HTTP 409"):
        return (
            f"Remote label {action} conflicted with a concurrent edit: {error}. "
            "Retry to apply the change against the latest version."
        )
    return f"Remote label {action} failed: {error}"


_REMOTE_SESSION_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REMOTE_ARTIFACT_HASH_PATTERN = re.compile(r"^[0-9a-f]{8,64}$")


def _normalize_remote_entity_type(entity_type: str) -> str:
    normalized = str(entity_type or "").strip().lower()
    # Composite artifacts are labeled as artifact targets by their composite hash.
    if normalized == "composite":
        return "artifact"
    return normalized


def _remote_target_params(
    entity_type: str,
    target: str,
    session_hash: str | None,
) -> dict[str, str]:
    """Validate remote identifiers and build the GLaaS label target params."""
    entity = _normalize_remote_entity_type(entity_type)
    normalized_target = str(target or "").strip()
    normalized_session = str(session_hash or "").strip().lower() or None
    if normalized_session and not _REMOTE_SESSION_HASH_PATTERN.match(normalized_session):
        raise ValueError("--session must be a full 64-character hex session hash.")

    if entity == "dag":
        normalized_target = normalized_target.lower()
        if not _REMOTE_SESSION_HASH_PATTERN.match(normalized_target):
            raise ValueError(
                "Remote dag targets are full 64-character hex session hashes "
                "(the hash in the /dag/<hash> URL)."
            )
        if normalized_session and normalized_session != normalized_target:
            raise ValueError("--session conflicts with the dag target; pass one session hash.")
        return {"entity_type": "dag", "session_hash": normalized_target}

    if entity == "job":
        if not normalized_session:
            raise ValueError("Remote job targets require --session <session-hash>.")
        if not normalized_target:
            raise ValueError("Remote job targets require a job uid.")
        return {
            "entity_type": "job",
            "session_hash": normalized_session,
            "job_uid": normalized_target,
        }

    if entity == "artifact":
        normalized_target = normalized_target.lower()
        if not _REMOTE_ARTIFACT_HASH_PATTERN.match(normalized_target):
            raise ValueError(
                "Remote artifact targets are content hashes (at least 8 hex characters)."
            )
        params = {"entity_type": "artifact", "artifact_hash": normalized_target}
        if normalized_session:
            params["session_hash"] = normalized_session
        return params

    raise ValueError(f"Unsupported remote label entity type: {entity_type}")


def _reject_reserved_remote_label_keys(keys: list[str], *, operation: str) -> None:
    reserved = sorted({key for key in keys if is_reserved_system_label_path(key)})
    if reserved:
        raise ValueError(
            f"Reserved label keys cannot be {operation} manually: {', '.join(reserved)}"
        )


def _fetch_remote_current_label(
    client: GlaasClient,
    params: dict[str, str],
    *,
    action: str,
) -> dict[str, Any] | None:
    """Fetch the current remote label doc; None when the target has no labels.

    A 404 is ambiguous on its own: it means "no labels for this target yet"
    on an up-to-date server, but "this route doesn't exist" on an old,
    not-yet-upgraded one. Only the former should be swallowed into ``None``
    (which callers like `remote_set_labels` read as "create") — the latter
    must surface as a clear error instead of silently being treated as
    version 0 or "no remote labels found".
    """
    result, error = client.get_current_labels(params)
    if error:
        if error.startswith("HTTP 404"):
            if _is_missing_get_labels_route_error(error):
                raise ValueError(_missing_get_labels_route_error("/api/v1/labels/current"))
            return None
        raise ValueError(_map_remote_label_error(error, action=action))
    return result if isinstance(result, dict) else None


def _remote_reconcile_item(
    client: GlaasClient,
    params: dict[str, str],
    current: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    """Build the reconcile item target ref plus the envelope session hash."""
    entity = params["entity_type"]
    if entity == "dag":
        session_hash = params["session_hash"]
        return {"entity_type": "dag", "session_hash": session_hash}, session_hash

    if entity == "job":
        session_hash = params["session_hash"]
        return {
            "entity_type": "job",
            "session_hash": session_hash,
            "job_uid": params["job_uid"],
        }, session_hash

    resolved_session: str | None = params.get("session_hash")
    artifact_hash = params["artifact_hash"]
    if isinstance(current, dict):
        current_session = current.get("sessionHash")
        if not resolved_session and isinstance(current_session, str) and current_session:
            resolved_session = current_session
        current_artifact = current.get("artifactHash")
        if isinstance(current_artifact, str) and current_artifact:
            artifact_hash = current_artifact
    if not resolved_session:
        resolved_session, artifact_hash = _resolve_remote_artifact_session(client, artifact_hash)
    return {
        "entity_type": "artifact",
        "session_hash": resolved_session,
        "artifact_hash": artifact_hash,
    }, resolved_session


def _resolve_remote_artifact_session(
    client: GlaasClient,
    artifact_hash: str,
) -> tuple[str, str]:
    """Resolve an unlabeled artifact's session hash from the artifact record."""
    from ...core.exceptions import GlaasError

    try:
        artifact = client.get_artifact(artifact_hash)
    except GlaasError as exc:
        raise ValueError(f"Remote artifact not found: {artifact_hash} ({exc})") from exc

    resolved_hash = artifact.get("hash") if isinstance(artifact, dict) else None
    session = None
    if isinstance(artifact, dict):
        session = artifact.get("originalSessionHash") or artifact.get("original_session_hash")
    if not isinstance(session, str) or not session:
        raise ValueError(
            "Could not resolve the artifact's session automatically; pass --session <session-hash>."
        )
    return session, str(resolved_hash or artifact_hash)


def _post_remote_reconcile(
    client: GlaasClient,
    session_hash: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_hash": session_hash,
        "mode": "sync_user_labels",
        "dry_run": False,
        "prune": False,
        "labels": [item],
    }
    if client.publish_auth.scope_request:
        payload["scope"] = dict(client.publish_auth.scope_request)
    result, error = client.reconcile_labels(payload)
    if error:
        raise ValueError(_map_remote_label_error(error, action="edit"))
    return result if isinstance(result, dict) else {}


def remote_set_labels(request: RemoteLabelSetRequest) -> str:
    """Patch the remote label document for a target directly on GLaaS."""
    return build_remote_set_labels_summary(request).render()


def build_remote_set_labels_summary(request: RemoteLabelSetRequest) -> LabelCurrentSummary:
    params = _remote_target_params(request.entity_type, request.target, request.session_hash)
    patch = parse_label_pairs(request.pairs)
    _reject_reserved_remote_label_keys(
        [key for key, _value in flatten_label_metadata(patch)],
        operation="set",
    )

    client = _create_remote_label_client(request.cwd, action="edit")
    current = _fetch_remote_current_label(client, params, action="edit")
    item, session_hash = _remote_reconcile_item(client, params, current)
    item["metadata"] = patch
    item["base_version"] = int(current.get("version") or 0) if isinstance(current, dict) else 0

    result = _post_remote_reconcile(client, session_hash, item)
    return LabelCurrentSummary(
        heading=_render_sync_heading(result, dry_run=False, prefix="Updated remote labels"),
        entries=_build_sync_entries(result),
        empty_message="No label changes.",
    )


def remote_unset_labels(request: RemoteLabelUnsetRequest) -> str:
    """Delete label keys from a remote target directly on GLaaS."""
    return build_remote_unset_labels_summary(request).render()


def build_remote_unset_labels_summary(request: RemoteLabelUnsetRequest) -> LabelCurrentSummary:
    params = _remote_target_params(request.entity_type, request.target, request.session_hash)
    keys = sorted({str(key).strip() for key in request.keys if str(key).strip()})
    if not keys:
        raise ValueError("Specify at least one label key to unset.")
    _reject_reserved_remote_label_keys(keys, operation="deleted")

    client = _create_remote_label_client(request.cwd, action="edit")
    current = _fetch_remote_current_label(client, params, action="edit")
    if current is None:
        raise ValueError("No remote labels found for the target.")
    item, session_hash = _remote_reconcile_item(client, params, current)
    item["metadata"] = {}
    item["deleted_keys"] = keys
    item["base_version"] = int(current.get("version") or 0)

    result = _post_remote_reconcile(client, session_hash, item)
    return LabelCurrentSummary(
        heading=_render_sync_heading(result, dry_run=False, prefix="Removed remote labels"),
        entries=_build_sync_entries(result),
        empty_message="No label changes.",
    )


def remote_show_labels(request: RemoteLabelShowRequest) -> str:
    """Show the current remote label document for a target."""
    return build_remote_show_labels_summary(request).render()


def build_remote_show_labels_summary(request: RemoteLabelShowRequest) -> LabelCurrentSummary:
    params = _remote_target_params(request.entity_type, request.target, request.session_hash)
    client = _create_remote_label_client(request.cwd, action="show")
    current = _fetch_remote_current_label(client, params, action="show")
    if current is None:
        raise ValueError("No remote labels found for the target.")

    version = current.get("version")
    can_edit = current.get("canEdit")
    editability = "" if can_edit is None else (", editable" if can_edit else ", read-only")
    metadata = current.get("metadata")
    return _build_current_summary(
        metadata if isinstance(metadata, dict) else {},
        heading=f"Remote labels (version {version}{editability}):",
    )


def remote_label_history(request: RemoteLabelHistoryRequest) -> str:
    """Show all remote label versions for a target."""
    return build_remote_label_history_summary(request).render()


def build_remote_label_history_summary(request: RemoteLabelHistoryRequest) -> LabelHistorySummary:
    params = _remote_target_params(request.entity_type, request.target, request.session_hash)
    client = _create_remote_label_client(request.cwd, action="history")
    result, error = client.get_label_history(params)
    if error:
        if error.startswith("HTTP 404"):
            if _is_missing_get_labels_route_error(error):
                raise ValueError(_missing_get_labels_route_error("/api/v1/labels/history"))
            raise ValueError("No remote labels found for the target.")
        raise ValueError(_map_remote_label_error(error, action="history"))

    rows = result.get("labels") if isinstance(result, dict) else None
    versions: list[LabelHistoryVersionSummary] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata")
        versions.append(
            LabelHistoryVersionSummary(
                version=int(row.get("version") or 0),
                entries=_build_label_entries(metadata if isinstance(metadata, dict) else {}),
            )
        )
    return LabelHistorySummary(versions=versions)


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


def _render_sync_heading(result: Any, *, dry_run: bool, prefix: str | None = None) -> str:
    payload = result if isinstance(result, dict) else {}
    if prefix is None:
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
