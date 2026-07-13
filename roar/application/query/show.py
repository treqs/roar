"""Application orchestration for the show query."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ...core.logging import get_logger
from ...core.step_name import resolve_step_name
from ...db.context import optional_repo
from ...db.query_context import create_query_database_context
from ...integrations.glaas import GlaasClient
from ...presenters.show_renderer import ShowRenderer
from ..lookup import (
    ArtifactRemoteLookupOperation,
    LookupSource,
    RefKind,
    lookup_remote_artifact,
    parse_ref,
    remote_artifact_fallback_enabled,
    run_local_then_remote_lookup,
)
from ..system_labels import omit_display_system_labels
from .requests import ShowQueryRequest
from .results import (
    ShowArtifactComponentSummary,
    ShowArtifactJobSummary,
    ShowArtifactLocationSummary,
    ShowArtifactSummary,
    ShowHashSummary,
    ShowJobArtifactSummary,
    ShowJobSummary,
    ShowSessionJobSummary,
    ShowSessionSummary,
    ShowSummary,
)

_NO_ACTIVE_SESSION_MESSAGE = "No active session. Run 'roar run' to create a session first."


class ShowQueryError(RuntimeError):
    """Raised when a show query cannot build a summary."""


def render_show(request: ShowQueryRequest) -> str:
    """Render session, job, or artifact details."""
    text, _summary = render_show_with_summary(request)
    return text


def render_show_with_summary(request: ShowQueryRequest) -> tuple[str, ShowSummary]:
    """Render and also return the typed summary so callers can act on
    its kind (e.g. emit an artifact-specific hint after the output)."""
    summary = build_show_summary(request)

    renderer = ShowRenderer(show_all=request.show_all)
    if isinstance(summary, ShowSessionSummary):
        session, jobs, labels = summary.to_renderer_args()
        return renderer.render_session(session, jobs, labels=labels), summary
    if isinstance(summary, ShowJobSummary):
        job, inputs, outputs, labels = summary.to_renderer_args()
        return renderer.render_job(job, inputs, outputs, labels=labels), summary

    artifact, locations, related_jobs, labels, composite_summary, components = (
        summary.to_renderer_args()
    )
    text = renderer.render_artifact(
        artifact,
        locations,
        related_jobs,
        labels=labels,
        composite_summary=composite_summary,
        components=components,
    )
    return text, summary


def build_show_summary(request: ShowQueryRequest) -> ShowSummary:
    """Build a typed show summary for session, job, or artifact details."""
    logger = _logger()
    if logger:
        logger.debug("show: entry with ref=%r selector=%r", request.ref, request.selector)

    with create_query_database_context(request.roar_dir) as db_ctx:
        if request.selector == "session":
            return _build_session_summary_for_ref(db_ctx, request.session_ref)

        if request.selector == "job":
            if request.ref is None:
                raise ShowQueryError("Job reference is required.")
            return _build_job_summary_for_ref(db_ctx, request.ref, request.session_ref)

        if request.selector == "path":
            if request.ref is None:
                raise ShowQueryError("Artifact path is required.")
            return _build_artifact_summary_for_path(db_ctx, request.cwd, request.ref)

        if request.selector == "artifact":
            if request.ref is None:
                raise ShowQueryError("Artifact hash is required.")
            parsed_ref = parse_ref(request.ref, selector=request.selector)
            return _build_artifact_summary_for_hash(
                db_ctx,
                request,
                request.ref,
                parsed_ref=parsed_ref,
            )

        if request.ref is None:
            return _build_session_summary_for_ref(db_ctx, request.session_ref)

        parsed_ref = parse_ref(request.ref, selector=request.selector)
        if logger:
            logger.debug(
                "show: ref_kind=%r selector=%r for ref=%r",
                parsed_ref.kind.value,
                parsed_ref.selector,
                request.ref,
            )

        if parsed_ref.kind == RefKind.JOB_STEP:
            return _build_job_summary_for_ref(db_ctx, request.ref)

        if parsed_ref.kind == RefKind.FILE_PATH:
            return _build_artifact_summary_for_path(db_ctx, request.cwd, request.ref)

        if parsed_ref.kind == RefKind.JOB_UID:
            return _build_job_summary_for_ref(db_ctx, request.ref)

        if parsed_ref.kind == RefKind.ARTIFACT_HASH:
            job = db_ctx.jobs.get_by_uid(request.ref)
            if job:
                return _build_job_summary(db_ctx, job)
            return _build_artifact_summary_for_hash(
                db_ctx,
                request,
                request.ref,
                parsed_ref=parsed_ref,
                missing_prefix="Not found",
            )

        artifact = _lookup_artifact_by_path(db_ctx, request.cwd, request.ref)
        if artifact:
            return _build_artifact_summary(db_ctx, artifact)

        raise ShowQueryError(f"No artifact found for path: {request.ref}")


def _logger():
    return get_logger()


def _safe_json_loads(raw: str, context: str) -> dict | None:
    logger = _logger()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        if logger:
            logger.debug("show: failed to decode %s JSON: %s", context, exc)
        return None
    if not isinstance(parsed, dict):
        if logger:
            logger.debug(
                "show: expected object for %s JSON, got %s", context, type(parsed).__name__
            )
        return None
    return parsed


def _path_present(path: str | None) -> bool | None:
    """`Path.exists()` with safe handling.

    Returns None when there's no path to check (so the renderer skips
    the `(missing)` marker for remote-only artifacts). Returns the
    actual boolean otherwise; any unexpected error is treated as
    "couldn't check" (also None) — we don't want a permissions glitch
    to flag a real file as missing.
    """
    if not path:
        return None
    try:
        return Path(path).exists()
    except OSError:
        return None


def _lookup_artifact_by_path(db_ctx, cwd: Path, ref: str) -> dict[str, Any] | None:
    path_obj = Path(os.path.expanduser(ref))
    if not path_obj.is_absolute():
        path_obj = cwd / path_obj
    resolved_path = os.path.normpath(str(path_obj.absolute()))
    return db_ctx.artifacts.get_by_path(resolved_path)


def _resolve_session(db_ctx, session_ref: str | None) -> dict | None:
    """Resolve a session ref to a session row.

    `None` → the active session. Otherwise full-hash match, then hash-prefix
    match — same precedence as `roar dag --session <ref>`.
    """
    if session_ref is None:
        return db_ctx.sessions.get_active()
    return db_ctx.sessions.get_by_hash(session_ref) or db_ctx.sessions.get_by_hash_prefix(
        session_ref
    )


def _build_session_summary_for_ref(db_ctx, session_ref: str | None) -> ShowSessionSummary:
    session = _resolve_session(db_ctx, session_ref)
    if not session:
        if session_ref is None:
            raise ShowQueryError(_NO_ACTIVE_SESSION_MESSAGE)
        raise ShowQueryError(f"No session found matching: {session_ref}")
    return _build_session_summary(db_ctx, session)


def _build_job_summary_for_ref(db_ctx, ref: str, session_ref: str | None = None) -> ShowJobSummary:
    if ref.startswith("@"):
        session = _resolve_session(db_ctx, session_ref)
        if not session:
            if session_ref is None:
                raise ShowQueryError(_NO_ACTIVE_SESSION_MESSAGE)
            raise ShowQueryError(f"No session found matching: {session_ref}")
        job = _resolve_job_ref(db_ctx, int(session["id"]), ref)
    else:
        job = db_ctx.jobs.get_by_uid(ref)

    if not job:
        raise ShowQueryError(f"Job not found: {ref}")
    return _build_job_summary(db_ctx, job)


def _build_artifact_summary_for_path(db_ctx, cwd: Path, ref: str) -> ShowArtifactSummary:
    artifact = _lookup_artifact_by_path(db_ctx, cwd, ref)
    if not artifact:
        raise ShowQueryError(f"No artifact found for path: {ref}")
    return _build_artifact_summary(db_ctx, artifact)


def _build_artifact_summary_for_hash(
    db_ctx,
    request: ShowQueryRequest,
    ref: str,
    *,
    parsed_ref,
    missing_prefix: str = "Artifact not found",
) -> ShowArtifactSummary:
    allow_remote = request.force_remote or remote_artifact_fallback_enabled(
        ArtifactRemoteLookupOperation.SHOW,
        parsed_ref,
        start_dir=request.cwd,
    )
    # Only stand up a GlaasClient (auth/config resolution) when remote
    # fallback can actually fire — the common local-hit path shouldn't pay
    # for it. The lambda below is only invoked by the runner when
    # allow_remote is True, so `client` is never None where it's used.
    client = GlaasClient(start_dir=str(request.cwd)) if allow_remote else None
    lookup = run_local_then_remote_lookup(
        lookup_local=lambda: db_ctx.artifacts.get_by_hash(ref),
        lookup_remote=lambda: lookup_remote_artifact(hash_prefix=ref, artifact_reader=client),
        allow_remote=allow_remote,
    )
    if lookup.error:
        raise ShowQueryError(lookup.error)
    if lookup.value is None:
        raise ShowQueryError(f"{missing_prefix}: {ref}")
    if lookup.source == LookupSource.LOCAL:
        return _build_artifact_summary(db_ctx, lookup.value)
    assert client is not None  # allow_remote was True to reach LookupSource.REMOTE
    return _build_remote_artifact_summary(lookup.value, client=client)


def _resolve_job_ref(db_ctx, session_id: int, job_ref: str) -> dict | None:
    if job_ref.startswith("@"):
        ref = job_ref[1:]
        job_type = None
        if ref.startswith("B"):
            job_type = "build"
            ref = ref[1:]
        try:
            step_number = int(ref)
        except ValueError:
            return None
        return db_ctx.sessions.get_step_by_number(session_id, step_number, job_type)
    return db_ctx.jobs.get_by_uid(job_ref)


def _prepare_job_for_render(job: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(job)
    if prepared.get("metadata") and isinstance(prepared["metadata"], str):
        prepared["metadata"] = _safe_json_loads(prepared["metadata"], "metadata")
    if prepared.get("telemetry") and isinstance(prepared["telemetry"], str):
        prepared["telemetry"] = _safe_json_loads(prepared["telemetry"], "telemetry")
    return prepared


def _build_session_summary(db_ctx, session: dict[str, Any]) -> ShowSessionSummary:
    jobs = db_ctx.jobs.get_by_session(session["id"], limit=100)
    labels = _current_label_metadata(db_ctx, "dag", session_id=int(session["id"]))
    return ShowSessionSummary(
        hash=str(session["hash"]),
        created_at=session["created_at"],
        git_repo=session.get("git_repo"),
        git_commit_start=session.get("git_commit_start"),
        labels=labels,
        jobs=[
            ShowSessionJobSummary(
                step_number=job.get("step_number"),
                job_uid=job.get("job_uid"),
                exit_code=job.get("exit_code"),
                command=job.get("command"),
                job_type=job.get("job_type"),
            )
            for job in jobs
        ],
    )


def _build_job_summary(db_ctx, job: dict[str, Any]) -> ShowJobSummary:
    inputs = db_ctx.jobs.get_inputs(job["id"])
    outputs = db_ctx.jobs.get_outputs(job["id"])
    prepared = _prepare_job_for_render(job)
    labels = _current_label_metadata(db_ctx, "job", job_id=int(job["id"]))
    return ShowJobSummary(
        job_uid=str(prepared["job_uid"]),
        step_number=prepared.get("step_number"),
        job_type=prepared.get("job_type"),
        step_name=resolve_step_name(labels, prepared.get("step_name")),
        step_identity=prepared.get("step_identity"),
        timestamp=prepared["timestamp"],
        duration_seconds=prepared.get("duration_seconds"),
        exit_code=prepared.get("exit_code"),
        command=prepared.get("command"),
        git_commit=prepared.get("git_commit"),
        git_branch=prepared.get("git_branch"),
        metadata=cast(dict[str, Any] | None, prepared.get("metadata")),
        telemetry=cast(dict[str, Any] | None, prepared.get("telemetry")),
        labels=labels,
        inputs=[_build_job_artifact_summary(artifact) for artifact in inputs],
        outputs=[_build_job_artifact_summary(artifact) for artifact in outputs],
    )


def _build_job_artifact_summary(artifact: dict[str, Any]) -> ShowJobArtifactSummary:
    path = str(artifact["path"])
    kind = cast(str | None, artifact.get("kind"))
    # A composite's "path" is a logical identifier (often a remote hf://… URI),
    # not a local file, and its components may be intentionally only partially
    # materialized (e.g. `roar get --limit`). A filesystem presence check is
    # meaningless and would always read "(missing)", so skip it (present=None).
    present = None if kind == "composite" else _path_present(path)
    return ShowJobArtifactSummary(
        path=path,
        artifact_id=str(artifact["artifact_id"]),
        kind=kind,
        component_count=cast(int | None, artifact.get("component_count")),
        size=int(artifact["size"]),
        hashes=[
            ShowHashSummary(
                algorithm=str(hash_entry["algorithm"]),
                digest=str(hash_entry["digest"]),
            )
            for hash_entry in cast(list[dict[str, Any]], artifact.get("hashes", []))
        ],
        present=present,
    )


def _build_artifact_summary(db_ctx, artifact: dict[str, Any]) -> ShowArtifactSummary:
    locations = db_ctx.artifacts.get_locations(artifact["id"])
    jobs = db_ctx.artifacts.get_jobs(artifact["id"])
    labels = _current_label_metadata(db_ctx, "artifact", artifact_id=str(artifact["id"]))

    composite_summary = None
    components = None
    composite_repo = cast(Any, optional_repo(db_ctx, "composites"))
    if composite_repo is not None:
        summary = composite_repo.get(artifact["id"])
        if isinstance(summary, dict):
            composite_summary = summary
            components = composite_repo.get_components(artifact["id"], limit=10) or None

    metadata = cast(dict[str, Any] | None, artifact.get("metadata"))
    if isinstance(metadata, str):
        metadata = _safe_json_loads(metadata, "artifact metadata")

    first_seen_path = cast(str | None, artifact.get("first_seen_path"))
    return ShowArtifactSummary(
        id=str(artifact["id"]),
        kind=cast(str | None, artifact.get("kind")),
        component_count=cast(int | None, artifact.get("component_count")),
        size=int(artifact["size"]),
        first_seen_at=artifact["first_seen_at"],
        first_seen_path=first_seen_path,
        first_seen_present=_path_present(first_seen_path),
        labels=labels,
        composite_summary=cast(dict[str, Any] | None, composite_summary),
        metadata=metadata,
        hashes=[
            ShowHashSummary(
                algorithm=str(hash_entry["algorithm"]),
                digest=str(hash_entry["digest"]),
            )
            for hash_entry in cast(list[dict[str, Any]], artifact.get("hashes", []))
        ],
        locations=[
            ShowArtifactLocationSummary(
                path=str(location["path"]),
                present=_path_present(str(location["path"])),
            )
            for location in locations
        ],
        produced_by=[
            ShowArtifactJobSummary(
                job_uid=cast(str | None, job_summary.get("job_uid")),
                command=cast(str | None, job_summary.get("command")),
                session_hash=cast(str | None, job_summary.get("session_hash")),
            )
            for job_summary in cast(list[dict[str, Any]], jobs.get("produced_by", []))
        ],
        consumed_by=[
            ShowArtifactJobSummary(
                job_uid=cast(str | None, job_summary.get("job_uid")),
                command=cast(str | None, job_summary.get("command")),
                session_hash=cast(str | None, job_summary.get("session_hash")),
            )
            for job_summary in cast(list[dict[str, Any]], jobs.get("consumed_by", []))
        ],
        components=[
            ShowArtifactComponentSummary(
                relative_path=cast(str | None, component.get("relative_path")),
                component_digest=cast(str | None, component.get("component_digest")),
                leaf_kind=cast(str | None, component.get("leaf_kind")),
            )
            for component in cast(list[dict[str, Any]], components or [])
        ],
    )


def _merge_remote_labels(raw_labels: Any) -> dict[str, Any] | None:
    """Flatten the array of scoped label-version records GLaaS returns
    into one key=value dict — the same shape `_current_label_metadata`
    produces locally. Each entry's `metadata` is merged in response order;
    a key visible in more than one scope is overwritten by the later entry
    (rare — most artifacts have labels in a single visible scope)."""
    if not isinstance(raw_labels, list):
        return None
    merged: dict[str, Any] = {}
    for entry in raw_labels:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get("metadata")
        if isinstance(metadata, dict):
            merged.update(metadata)
    if not merged:
        return None
    return cast(dict[str, Any] | None, omit_display_system_labels(merged))


def _remote_owner_and_visibility(artifact: dict[str, Any]) -> tuple[str | None, str | None]:
    scope = artifact.get("scope")
    if not isinstance(scope, dict):
        return None, None
    owner = scope.get("owner_name") or scope.get("owner_id")
    project = scope.get("project_name") or scope.get("project_id")
    owner_str = "/".join(str(part) for part in (owner, project) if part) or None
    visibility = cast("str | None", scope.get("visibility"))
    return owner_str, visibility


def _remote_produced_by(client: GlaasClient, hash_prefix: str) -> list[ShowArtifactJobSummary]:
    """Best-effort: the producing job, via the lineage endpoint at depth 1.

    Errors (including 401/403 for a caller who can't see the producing job)
    are swallowed — the renderer already treats an empty produced_by as
    nothing-to-show, same as a local artifact with no recorded producer.
    """
    lineage, error = client.get_artifact_lineage(hash_prefix, depth=1)
    if error or not isinstance(lineage, dict):
        return []
    producer = lineage.get("producedBy")
    if not isinstance(producer, dict):
        return []
    return [
        ShowArtifactJobSummary(
            job_uid=cast("str | None", producer.get("jobUid")),
            command=cast("str | None", producer.get("command")),
            session_hash=cast("str | None", producer.get("sessionHash")),
        )
    ]


def _remote_components(client: GlaasClient, hash_prefix: str) -> list[ShowArtifactComponentSummary]:
    """Best-effort composite component listing; same error-swallowing as
    `_remote_produced_by` (the components endpoint requires auth, so an
    anonymous caller cleanly gets an empty list instead of an error)."""
    result, error = client.get_composite_components(hash_prefix)
    if error or not isinstance(result, dict):
        return []
    raw_components = result.get("components")
    if not isinstance(raw_components, list):
        return []
    return [
        ShowArtifactComponentSummary(
            relative_path=cast("str | None", component.get("relativePath")),
            component_digest=cast("str | None", component.get("componentDigest")),
            leaf_kind=cast("str | None", component.get("leafKind")),
        )
        for component in raw_components[:10]
        if isinstance(component, dict)
    ]


def _build_remote_artifact_summary(
    artifact: dict[str, Any], *, client: GlaasClient
) -> ShowArtifactSummary:
    metadata = cast(dict[str, Any] | None, artifact.get("metadata"))
    if isinstance(metadata, str):
        metadata = _safe_json_loads(metadata, "remote artifact metadata")

    artifact_hash = cast(str | None, artifact.get("hash") or artifact.get("id"))
    hashes = cast(list[dict[str, Any]], artifact.get("hashes", []))
    if not hashes and artifact_hash:
        hashes = [{"algorithm": "blake3", "digest": artifact_hash}]

    is_composite = artifact.get("isComposite") is True
    kind = cast(str | None, artifact.get("kind"))
    if kind is None and is_composite:
        kind = "composite"

    remote_owner, remote_visibility = _remote_owner_and_visibility(artifact)

    produced_by: list[ShowArtifactJobSummary] = []
    components: list[ShowArtifactComponentSummary] = []
    if artifact_hash:
        produced_by = _remote_produced_by(client, artifact_hash)
        if is_composite or kind == "composite":
            components = _remote_components(client, artifact_hash)

    return ShowArtifactSummary(
        id=artifact_hash or "remote-artifact",
        source=LookupSource.REMOTE.value,
        kind=kind,
        size=_coerce_int(artifact.get("size")),
        first_seen_at=_parse_remote_timestamp(
            artifact.get("registeredAt") or artifact.get("registered_at")
        ),
        labels=_merge_remote_labels(artifact.get("labels")),
        remote_owner=remote_owner,
        remote_visibility=remote_visibility,
        metadata=metadata,
        hashes=[
            ShowHashSummary(
                algorithm=str(hash_entry["algorithm"]),
                digest=str(hash_entry["digest"]),
            )
            for hash_entry in hashes
            if isinstance(hash_entry, dict)
            and isinstance(hash_entry.get("algorithm"), str)
            and isinstance(hash_entry.get("digest"), str)
        ],
        produced_by=produced_by,
        components=components,
    )


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_remote_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def _current_label_metadata(
    db_ctx,
    entity_type: str,
    *,
    session_id: int | None = None,
    job_id: int | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any] | None:
    labels_repo = cast(Any, optional_repo(db_ctx, "labels"))
    if labels_repo is None:
        return None

    current = labels_repo.get_current(
        entity_type,
        session_id=session_id,
        job_id=job_id,
        artifact_id=artifact_id,
    )
    if not isinstance(current, dict):
        return None

    metadata = current.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return cast(dict[str, Any] | None, omit_display_system_labels(metadata))
