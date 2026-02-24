"""
Native Click implementation of the show command.

Usage: roar show [REF]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import click

from ...core.bootstrap import bootstrap
from ...core.di import try_resolve
from ...core.interfaces.logger import ILogger
from ...db.context import create_database_context, optional_repo
from ...presenters.show_renderer import ShowRenderer
from ..context import RoarContext
from ..decorators import require_init


def _logger() -> ILogger | None:
    """Resolve logger from DI container when available."""
    return try_resolve(ILogger)  # type: ignore[type-abstract]


def _safe_json_loads(raw: str, context: str) -> dict | None:
    """Parse JSON with debug logging instead of silent failure."""
    logger = _logger()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        if logger:
            logger.debug("show: failed to decode %s JSON: %s", context, e)
        return None
    if not isinstance(parsed, dict):
        if logger:
            logger.debug(
                "show: expected object for %s JSON, got %s", context, type(parsed).__name__
            )
        return None
    return parsed


def _classify_ref(ref: str, cwd: Path) -> str:
    """
    Classify reference type: job_step, file_path, job_uid, or artifact_hash.

    Resolution order:
    - @N or @BN: Job step
    - Contains / or starts with ./, ../, ~: File path
    - File exists in cwd: File path
    - Hex string <= 8 chars: Job UID
    - Hex string > 8 chars: Artifact hash (will try job first, fallback to artifact)

    Args:
        ref: The reference string to classify
        cwd: Current working directory for file existence checks

    Returns:
        One of: "job_step", "file_path", "job_uid", "artifact_hash", "unknown"
    """
    logger = _logger()
    if logger:
        logger.debug("_classify_ref: ref=%r, cwd=%s", ref, cwd)

    if ref.startswith("@"):
        if logger:
            logger.debug("_classify_ref: matched job_step (starts with @)")
        return "job_step"
    if "/" in ref or ref.startswith(("./", "../", "~")):
        if logger:
            logger.debug("_classify_ref: matched file_path (contains / or starts with ./, ../, ~)")
        return "file_path"
    file_exists = (cwd / ref).exists()
    if logger:
        logger.debug("_classify_ref: file exists check for %r: %s", str(cwd / ref), file_exists)
    if file_exists:
        if logger:
            logger.debug("_classify_ref: matched file_path (file exists in cwd)")
        return "file_path"
    is_hex = all(c in "0123456789abcdefABCDEF" for c in ref)
    if logger:
        logger.debug("_classify_ref: is_hex=%s, len=%d", is_hex, len(ref))
    if is_hex and len(ref) <= 8:
        if logger:
            logger.debug("_classify_ref: matched job_uid (hex string <= 8 chars)")
        return "job_uid"
    if is_hex and len(ref) > 8:
        if logger:
            logger.debug("_classify_ref: matched artifact_hash (hex string > 8 chars)")
        return "artifact_hash"
    if logger:
        logger.debug("_classify_ref: no match, returning unknown")
    return "unknown"


def _resolve_job_ref(db_ctx, session_id: int, job_ref: str) -> dict | None:
    """
    Resolve a job reference to a job dict.

    Accepts:
    - @N or @BN notation (step number)
    - job_uid (full or prefix)
    """
    logger = _logger()
    if logger:
        logger.debug("_resolve_job_ref: job_ref=%r, session_id=%d", job_ref, session_id)

    # Handle @N notation
    if job_ref.startswith("@"):
        ref = job_ref[1:]
        job_type = None
        if ref.startswith("B"):
            job_type = "build"
            ref = ref[1:]
        if logger:
            logger.debug("_resolve_job_ref: @N notation, ref=%r, job_type=%s", ref, job_type)
        try:
            step_number = int(ref)
            if logger:
                logger.debug("_resolve_job_ref: looking up step_number=%d", step_number)
            job = db_ctx.sessions.get_step_by_number(session_id, step_number, job_type)
            if logger:
                logger.debug(
                    "_resolve_job_ref: step lookup result=%s", "found" if job else "not found"
                )
            return job
        except ValueError:
            if logger:
                logger.debug("_resolve_job_ref: failed to parse step number from %r", ref)
            return None

    # Handle job_uid lookup
    if logger:
        logger.debug("_resolve_job_ref: looking up job by uid=%r", job_ref)
    job = db_ctx.jobs.get_by_uid(job_ref)
    if logger:
        logger.debug("_resolve_job_ref: uid lookup result=%s", "found" if job else "not found")
    return job


def _prepare_job_for_render(job: dict) -> dict:
    """Parse raw JSON fields on a job dict into Python objects for the renderer.

    The renderer expects 'metadata' and 'telemetry' as parsed dicts (or None),
    not raw JSON strings.  This function returns a shallow copy of *job* with
    those two keys replaced by their parsed equivalents.
    """
    job = dict(job)  # shallow copy so we don't mutate the original
    if job.get("metadata") and isinstance(job["metadata"], str):
        job["metadata"] = _safe_json_loads(job["metadata"], "metadata")
    if job.get("telemetry") and isinstance(job["telemetry"], str):
        job["telemetry"] = _safe_json_loads(job["telemetry"], "telemetry")
    return job


def _show_session(db_ctx, session: dict) -> None:
    """Fetch data and display session-level view with all jobs."""
    jobs = db_ctx.jobs.get_by_session(session["id"], limit=100)
    renderer = ShowRenderer()
    click.echo(renderer.render_session(session, jobs))


def _show_job(db_ctx, job: dict) -> None:
    """Fetch data and display detailed job view with artifacts."""
    inputs = db_ctx.jobs.get_inputs(job["id"])
    outputs = db_ctx.jobs.get_outputs(job["id"])
    prepared_job = _prepare_job_for_render(job)
    renderer = ShowRenderer()
    click.echo(renderer.render_job(prepared_job, inputs, outputs))


def _show_artifact(db_ctx, artifact: dict) -> None:
    """Fetch data and display detailed artifact view."""
    locations = db_ctx.artifacts.get_locations(artifact["id"])
    jobs = db_ctx.artifacts.get_jobs(artifact["id"])

    composite_summary = None
    components = None
    composite_repo = cast(Any, optional_repo(db_ctx, "composites"))
    if composite_repo is not None:
        summary = composite_repo.get(artifact["id"])
        if isinstance(summary, dict):
            composite_summary = summary
            components = composite_repo.get_components(artifact["id"], limit=10) or None

    renderer = ShowRenderer()
    click.echo(renderer.render_artifact(artifact, locations, jobs, composite_summary, components))


@click.command("show")
@click.argument("ref", required=False)
@click.pass_obj
@require_init
def show(ctx: RoarContext, ref: str | None) -> None:
    """Show session, job, or artifact details.

    Without arguments, displays the active session and its jobs.
    With a reference, displays detailed information based on the reference type.

    \b
    REF can be:
      - @N or @BN: Job by step number (e.g., @1, @B2)
      - 8-char hex: Job by UID
      - Longer hex: Artifact by hash (falls back to job if found)
      - File path: Artifact at that path (e.g., ./output/model.pkl)

    \b
    Examples:
        roar show                          # Show active session overview
        roar show @1                       # Show details for step 1
        roar show @B1                      # Show details for build step 1
        roar show a1b2c3d4                 # Show job by UID
        roar show a1b2c3d4e5f67890...      # Show artifact by hash
        roar show ./output/model.pkl       # Show artifact by path
    """
    bootstrap(ctx.roar_dir)
    logger = _logger()
    if logger:
        logger.debug("show: entry with ref=%r", ref)

    with create_database_context(ctx.roar_dir) as db_ctx:
        if ref is None:
            if logger:
                logger.debug("show: no ref provided, showing active session")
            session = db_ctx.sessions.get_active()
            if logger:
                logger.debug("show: active session=%s", "found" if session else "not found")
            if not session:
                click.echo("No active session.")
                return
            if logger:
                logger.debug("show: calling _show_session")
            _show_session(db_ctx, session)
            return

        ref_type = _classify_ref(ref, ctx.cwd)
        if logger:
            logger.debug("show: ref_type=%r for ref=%r", ref_type, ref)

        if ref_type == "job_step":
            if logger:
                logger.debug("show: handling job_step branch")
            session = db_ctx.sessions.get_active()
            if logger:
                logger.debug("show: active session=%s", "found" if session else "not found")
            if not session:
                click.echo("No active session.")
                return
            job = _resolve_job_ref(db_ctx, session["id"], ref)
            if logger:
                logger.debug("show: job resolution=%s", "found" if job else "not found")
            if not job:
                click.echo(f"Job not found: {ref}")
                return
            if logger:
                logger.debug("show: calling _show_job for job_uid=%s", job.get("job_uid"))
            _show_job(db_ctx, job)

        elif ref_type == "file_path":
            if logger:
                logger.debug("show: handling file_path branch")
            # Resolve to absolute path, expanding ~ and normalizing ./ and ../
            path_obj = Path(os.path.expanduser(ref))
            if logger:
                logger.debug("show: original ref=%r, expanded path=%s", ref, path_obj)
            if not path_obj.is_absolute():
                path_obj = ctx.cwd / path_obj
            path = os.path.normpath(path_obj.absolute())
            if logger:
                logger.debug("show: resolved absolute path=%s", path)
            artifact = db_ctx.artifacts.get_by_path(path)
            if logger:
                logger.debug(
                    "show: artifact lookup by path=%s", "found" if artifact else "not found"
                )
            if not artifact:
                click.echo(f"No artifact found for path: {ref}")
                return
            if logger:
                logger.debug("show: calling _show_artifact for artifact_id=%s", artifact.get("id"))
            _show_artifact(db_ctx, artifact)

        elif ref_type == "job_uid":
            if logger:
                logger.debug("show: handling job_uid branch")
            job = db_ctx.jobs.get_by_uid(ref)
            if logger:
                logger.debug("show: job lookup by uid=%s", "found" if job else "not found")
            if not job:
                click.echo(f"Job not found: {ref}")
                return
            if logger:
                logger.debug("show: calling _show_job for job_uid=%s", job.get("job_uid"))
            _show_job(db_ctx, job)

        elif ref_type == "artifact_hash":
            if logger:
                logger.debug("show: handling artifact_hash branch")
            # Try job first (preserves existing behavior for 8+ char job UIDs)
            if logger:
                logger.debug("show: trying job lookup first for ref=%r", ref)
            job = db_ctx.jobs.get_by_uid(ref)
            if logger:
                logger.debug("show: job lookup by uid=%s", "found" if job else "not found")
            if job:
                if logger:
                    logger.debug("show: calling _show_job for job_uid=%s", job.get("job_uid"))
                _show_job(db_ctx, job)
                return
            if logger:
                logger.debug("show: trying artifact lookup for ref=%r", ref)
            artifact = db_ctx.artifacts.get_by_hash(ref)
            if logger:
                logger.debug(
                    "show: artifact lookup by hash=%s", "found" if artifact else "not found"
                )
            if artifact:
                if logger:
                    logger.debug(
                        "show: calling _show_artifact for artifact_id=%s", artifact.get("id")
                    )
                _show_artifact(db_ctx, artifact)
                return
            click.echo(f"Not found: {ref}")

        else:
            if logger:
                logger.debug("show: unknown ref_type=%r", ref_type)
            click.echo(f"Unknown reference format: {ref}")
