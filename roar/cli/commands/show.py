"""
Native Click implementation of the show command.

Usage: roar show [REF]
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from ...core.bootstrap import bootstrap
from ...core.container import get_container
from ...core.interfaces.logger import ILogger
from ...db.context import create_database_context
from ...presenters.formatting import format_duration, format_size, format_timestamp
from ..context import RoarContext
from ..decorators import require_init


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
    logger = get_container().try_resolve(ILogger)  # type: ignore[type-abstract]
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
    logger = get_container().try_resolve(ILogger)  # type: ignore[type-abstract]
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


def _show_session(db_ctx, session: dict) -> None:
    """Display session-level view with all jobs."""
    click.echo(f"\nSession: {session['hash']}")
    click.echo(f"Created: {format_timestamp(session['created_at'])}")
    if session.get("git_repo"):
        click.echo(f"Git: {session['git_repo']}")
    if session.get("git_commit_start"):
        click.echo(f"Commit: {session['git_commit_start']}")

    jobs = db_ctx.jobs.get_by_session(session["id"], limit=100)

    if not jobs:
        click.echo("\nNo jobs in this session.")
        return

    click.echo(f"\nJobs ({len(jobs)}):\n")

    # Header
    click.echo(f"{'STEP':<6}  {'JOB UID':<8}  {'STATUS':<6}  {'COMMAND'}")
    click.echo("-" * 60)

    # Jobs ordered by step number (oldest first)
    for job in reversed(jobs):
        step = f"@{job['step_number']}" if job["step_number"] else "-"
        if job.get("job_type") == "build":
            step = f"@B{job['step_number']}" if job["step_number"] else "-"

        uid = job["job_uid"] or "?"

        if job["exit_code"] is None:
            status = "?"
        elif job["exit_code"] == 0:
            status = "OK"
        else:
            status = "FAIL"

        command = job["command"] or ""
        # Truncate long commands for table display
        if len(command) > 50:
            command = command[:47] + "..."

        click.echo(f"{step:<6}  {uid:<8}  {status:<6}  {command}")


def _show_job(db_ctx, job: dict) -> None:
    """Display detailed job view with artifacts."""
    click.echo(f"\nJob: {job['job_uid']}")

    step_ref = ""
    if job["step_number"]:
        prefix = "@B" if job.get("job_type") == "build" else "@"
        step_ref = f" ({prefix}{job['step_number']})"
    click.echo(f"Step: {job['step_number'] or '-'}{step_ref}")

    if job.get("step_name"):
        click.echo(f"Name: {job['step_name']}")
    if job.get("step_identity"):
        click.echo(f"Identity: {job['step_identity']}")

    click.echo(f"Timestamp: {format_timestamp(job['timestamp'])}")
    click.echo(f"Duration: {format_duration(job['duration_seconds'])}")

    if job["exit_code"] is None:
        status = "Unknown"
    elif job["exit_code"] == 0:
        status = "Success"
    else:
        status = f"Failed (exit {job['exit_code']})"
    click.echo(f"Status: {status}")

    click.echo(f"\nCommand: {job['command']}")

    # Git info
    if job.get("git_commit"):
        click.echo(f"\nGit commit: {job['git_commit']}")
    if job.get("git_branch"):
        click.echo(f"Git branch: {job['git_branch']}")

    # Metadata (what gets registered with GLaaS)
    if job.get("metadata"):
        try:
            meta = json.loads(job["metadata"])
            if meta:
                click.echo("\nMetadata:")

                # Working directory
                if meta.get("cwd"):
                    click.echo(f"  Working dir: {meta['cwd']}")

                # Runtime info
                runtime = meta.get("runtime", {})
                if runtime.get("hostname"):
                    click.echo(f"  Hostname: {runtime['hostname']}")
                if runtime.get("os"):
                    os_info = runtime["os"]
                    click.echo(f"  OS: {os_info.get('system', '')} {os_info.get('release', '')}")
                if runtime.get("python"):
                    click.echo(f"  Python: {runtime['python'].get('version', '')}")

                # Hardware
                if runtime.get("gpu"):
                    gpus = runtime["gpu"]
                    for i, gpu in enumerate(gpus):
                        gpu_str = f"  GPU {i}: {gpu.get('name', 'unknown')} ({gpu.get('memory_mb', '?')} MB)"
                        if gpu.get("compute_cap"):
                            gpu_str += f", compute cap {gpu['compute_cap']}"
                        click.echo(gpu_str)
                if runtime.get("cuda"):
                    cuda = runtime["cuda"]
                    cuda_parts = []
                    if cuda.get("cuda_version"):
                        cuda_parts.append(f"CUDA {cuda['cuda_version']}")
                    if cuda.get("driver_version"):
                        cuda_parts.append(f"driver {cuda['driver_version']}")
                    if cuda.get("cudnn_version"):
                        cuda_parts.append(f"cuDNN {cuda['cudnn_version']}")
                    if cuda_parts:
                        click.echo(f"  CUDA: {', '.join(cuda_parts)}")
                if runtime.get("cpu"):
                    cpu = runtime["cpu"]
                    click.echo(
                        f"  CPU: {cpu.get('model', 'unknown')} ({cpu.get('count', '?')} cores)"
                    )
                if runtime.get("memory"):
                    mem = runtime["memory"]
                    click.echo(f"  Memory: {mem.get('total_mb', '?')} MB")

                # Environment variables
                env_vars = runtime.get("env_vars", {})
                if env_vars:
                    click.echo(f"\n  Environment Variables ({len(env_vars)}):")
                    for name, value in sorted(env_vars.items()):
                        # Truncate long values
                        display_val = value if len(value) <= 60 else value[:57] + "..."
                        click.echo(f"    {name}={display_val}")

                # Packages
                packages = meta.get("packages", {})
                if packages and isinstance(packages, dict):
                    for manager, pkgs in packages.items():
                        if pkgs and isinstance(pkgs, dict):
                            click.echo(f"\n  Packages ({manager}, {len(pkgs)}):")
                            for name, version in sorted(pkgs.items())[:15]:
                                if version:
                                    click.echo(f"    {name}=={version}")
                                else:
                                    click.echo(f"    {name}")
                            if len(pkgs) > 15:
                                click.echo(f"    ... and {len(pkgs) - 15} more")
        except json.JSONDecodeError:
            pass

    # Telemetry (external service links)
    if job.get("telemetry"):
        try:
            telem = json.loads(job["telemetry"])
            if telem:
                click.echo("\nTelemetry:")
                for name, url in telem.items():
                    if isinstance(url, list):
                        for u in url:
                            click.echo(f"  {name}: {u}")
                    else:
                        click.echo(f"  {name}: {url}")
        except json.JSONDecodeError:
            pass

    # Inputs
    inputs = db_ctx.jobs.get_inputs(job["id"], db_ctx.artifacts)
    if inputs:
        click.echo(f"\nInputs ({len(inputs)}):")
        for inp in inputs:
            click.echo(f"  {inp['path']}")
            click.echo(f"    Artifact: {inp['artifact_id']}")
            click.echo(f"    Size: {format_size(inp['size'])}")
            for h in inp.get("hashes", []):
                click.echo(f"    {h['algorithm']}: {h['digest']}")

    # Outputs
    outputs = db_ctx.jobs.get_outputs(job["id"], db_ctx.artifacts)
    if outputs:
        click.echo(f"\nOutputs ({len(outputs)}):")
        for out in outputs:
            click.echo(f"  {out['path']}")
            click.echo(f"    Artifact: {out['artifact_id']}")
            click.echo(f"    Size: {format_size(out['size'])}")
            for h in out.get("hashes", []):
                click.echo(f"    {h['algorithm']}: {h['digest']}")


def _show_artifact(db_ctx, artifact: dict) -> None:
    """Display detailed artifact view."""
    click.echo(f"\nArtifact: {artifact['id']}")
    click.echo(f"Size: {format_size(artifact['size'])}")
    click.echo(f"First seen: {format_timestamp(artifact['first_seen_at'])}")

    if artifact.get("first_seen_path"):
        click.echo(f"Original path: {artifact['first_seen_path']}")

    # Hashes
    hashes = artifact.get("hashes", [])
    if hashes:
        click.echo("\nHashes:")
        for h in hashes:
            click.echo(f"  {h['algorithm']}: {h['digest']}")

    # Locations
    locations = db_ctx.artifacts.get_locations(artifact["id"])
    if locations:
        click.echo(f"\nLocations ({len(locations)}):")
        for loc in locations:
            click.echo(f"  {loc['path']}")

    # Jobs
    jobs = db_ctx.artifacts.get_jobs(artifact["id"])
    produced_by = jobs.get("produced_by", [])
    if produced_by:
        click.echo(f"\nProduced by ({len(produced_by)} job(s)):")
        for job in produced_by[:5]:
            cmd = (job.get("command") or "?")[:47]
            click.echo(f"  [{job.get('job_uid', '?')}] {cmd}")

    consumed_by = jobs.get("consumed_by", [])
    if consumed_by:
        click.echo(f"\nConsumed by ({len(consumed_by)} job(s)):")
        for job in consumed_by[:5]:
            cmd = (job.get("command") or "?")[:47]
            click.echo(f"  [{job.get('job_uid', '?')}] {cmd}")


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
    logger = get_container().try_resolve(ILogger)  # type: ignore[type-abstract]
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
