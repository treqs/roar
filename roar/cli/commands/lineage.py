"""
Native Click implementation of the lineage command.

Usage: roar lineage [options] <artifact>
"""

import json
import os
from pathlib import Path

import click

from ...db.context import create_database_context
from ..context import RoarContext
from ..decorators import require_init


def _resolve_artifact_path(path: str, cwd: Path) -> str | None:
    """
    Resolve artifact path and compute its BLAKE3 hash.

    Args:
        path: File path (absolute or relative)
        cwd: Current working directory

    Returns:
        BLAKE3 hash of the file, or None if file doesn't exist.
    """
    # Make path absolute
    if not os.path.isabs(path):
        path = str(cwd / path)

    if not os.path.exists(path):
        return None

    # Compute hash using blake3
    try:
        import blake3

        b3_hasher = blake3.blake3()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192 * 1024), b""):
                b3_hasher.update(chunk)
        return b3_hasher.hexdigest()
    except ImportError:
        # Fallback to hashlib if blake3 not available
        import hashlib

        sha_hasher = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192 * 1024), b""):
                sha_hasher.update(chunk)
        return sha_hasher.hexdigest()
    except OSError:
        return None


@click.command("lineage", hidden=True)
@click.argument("artifact")
@click.option(
    "--output",
    "-o",
    type=click.Choice(["json"]),
    default="json",
    help="Output format (default: json)",
)
@click.option(
    "--depth",
    "-d",
    type=int,
    default=10,
    help="Maximum traversal depth (default: 10)",
)
@click.pass_obj
@require_init
def lineage(ctx: RoarContext, artifact: str, output: str, depth: int) -> None:
    """Show artifact lineage as JSON.

    Traces upstream through the job DAG to find all inputs and jobs
    that were needed to produce the target artifact.

    \b
    The ARTIFACT can be:
    - A file path (e.g., model.pt)
    - A hash prefix (e.g., abc123)

    \b
    Examples:

        roar lineage model.pt              # By file path

        roar lineage --output=json model.pt

        roar lineage abc123def             # By hash prefix

        roar lineage --depth=5 model.pt    # Limit depth
    """
    with create_database_context(ctx.roar_dir) as db_ctx:
        # Try to resolve as file path first
        artifact_hash = None
        file_path = None

        # Check if it looks like a path (contains / or . or exists as file)
        if "/" in artifact or os.path.exists(artifact):
            file_path = artifact
            artifact_hash = _resolve_artifact_path(artifact, ctx.cwd)
            if not artifact_hash:
                raise click.ClickException(f"File not found: {artifact}")
        else:
            # Try as hash prefix
            artifact_hash = artifact

        # Look up the artifact in the database
        db_artifact = db_ctx.artifacts.get_by_hash(artifact_hash, algorithm="blake3")
        if not db_artifact:
            # Try without algorithm filter
            db_artifact = db_ctx.artifacts.get_by_hash(artifact_hash)

        if not db_artifact:
            raise click.ClickException(
                f"Artifact not found in database: {artifact}\n"
                "The file may not have been tracked by roar yet."
            )

        # Get filtered lineage
        target_artifact, jobs, _on_path_hashes = db_ctx.lineage.get_filtered_lineage(
            db_artifact["id"], max_depth=depth
        )

        if not target_artifact:
            raise click.ClickException(f"Could not trace lineage for artifact: {artifact}")

        # Get target hash and path
        target_hash = None
        for h in target_artifact.get("hashes", []):
            if h.get("algorithm") == "blake3":
                target_hash = h.get("digest")
                break

        if not target_hash:
            raise click.ClickException("Artifact has no BLAKE3 hash")

        # Determine the path to use for target artifact
        target_path = file_path or target_artifact.get("first_seen_path", "")

        # Build jobs list
        jobs_list: list[dict] = []
        for job in jobs:
            job_info = {
                "job_uid": job.get("job_uid", ""),
                "step_number": job.get("step_number"),
                "command": job.get("command", ""),
                "timestamp": job.get("timestamp", 0),
                "duration_seconds": job.get("duration_seconds"),
                "exit_code": job.get("exit_code"),
                "inputs": job.get("_inputs", []),
                "outputs": job.get("_outputs", []),
            }
            jobs_list.append(job_info)

        # Build artifacts list - collect from jobs
        seen_hashes: set[str] = set()
        artifacts_list: list[dict] = []

        for job in jobs:
            for inp in job.get("_inputs", []):
                if inp["hash"] not in seen_hashes:
                    seen_hashes.add(inp["hash"])
                    artifacts_list.append(inp)
            for out in job.get("_outputs", []):
                if out["hash"] not in seen_hashes:
                    seen_hashes.add(out["hash"])
                    artifacts_list.append(out)

        # Add target artifact if not already in list
        if target_hash not in seen_hashes:
            artifacts_list.append(
                {
                    "hash": target_hash,
                    "path": target_path,
                    "size": target_artifact.get("size", 0),
                }
            )

        # Build the output
        result = {
            "artifact": {
                "path": target_path,
                "hash": target_hash,
                "size": target_artifact.get("size", 0),
            },
            "jobs": jobs_list,
            "artifacts": artifacts_list,
        }

        # Output JSON
        click.echo(json.dumps(result, indent=2))
