"""
Native Click implementation of the put command.

Usage: roar put [sources...] <destination> -m "message"

roar put ALWAYS registers lineage with GLaaS. If GLaaS is not configured,
the command will fail.
"""

from __future__ import annotations

import click

from ...application.publish.requests import PutRequest
from ...application.publish.service import put_artifacts
from ...config import config_get
from ..context import RoarContext
from ..decorators import require_init


@click.command("put")
@click.argument("args", nargs=-1, required=True)
@click.option(
    "-m",
    "--message",
    required=True,
    help="Publish message (required, like git commit -m).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be uploaded without doing it.",
)
@click.option(
    "--no-tag",
    is_flag=True,
    help="Skip creating and pushing git tag.",
)
@click.pass_obj
@require_init
def put(
    ctx: RoarContext,
    args: tuple[str, ...],
    message: str,
    dry_run: bool,
    no_tag: bool,
) -> None:
    """Publish artifacts to cloud storage and register with GLaaS.

    This is an atomic publish operation that:
    1. Uploads artifacts to cloud storage (S3, GCS, etc.)
    2. Registers artifacts and provenance with GLaaS
    3. Creates a git tag marking the published state

    The last argument is the destination URL. All preceding arguments are
    sources (files, directories, @N job references). If no sources are
    specified before the destination, all outputs from the current session
    are uploaded.

    \b
    Destination formats:
        s3://bucket/prefix     AWS S3
        gs://bucket/prefix     Google Cloud Storage

    \b
    Examples:

        # Upload all session outputs to S3
        roar put s3://my-bucket/run-42 -m "publish outputs"

        # Upload specific files
        roar put model.pt config.yaml s3://bucket/release -m "v1.0"

        # Upload a directory
        roar put outputs/ s3://bucket/outputs -m "publish all"

        # Upload outputs from step 2
        roar put @2 s3://bucket/step-2 -m "publish step 2"

        # Dry run to see what would be uploaded
        roar put outputs/ s3://bucket/test -m "test" --dry-run
    """
    if len(args) < 1:
        raise click.ClickException("Destination URL is required")

    # Last arg is destination, rest are sources
    destination = args[-1]
    sources = list(args[:-1])

    try:
        response = put_artifacts(
            PutRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                repo_root=ctx.repo_root,
                sources=sources,
                destination=destination,
                message=message,
                dry_run=dry_run,
                no_tag=no_tag,
            )
        )
    except (FileNotFoundError, ValueError, ImportError) as e:
        raise click.ClickException(str(e)) from e
    except Exception as e:  # pragma: no cover - defensive CLI boundary
        raise click.ClickException(f"Unexpected error during put: {e}") from e

    result = response.result

    for warning in response.warnings:
        click.echo(f"Warning: {warning}", err=True)

    # Handle dry run output
    if dry_run:
        click.echo("Dry run - would upload:")
        for item in result.would_upload:
            click.echo(f"  {item['path']}")
        click.echo(f"\nTotal: {len(result.would_upload)} file(s)")
        return

    # Check for registration errors
    if not result.success:
        click.echo(f"Published {len(result.uploaded_files)} file(s) to {destination}")
        click.echo("\nWarning: Registration completed with errors:", err=True)
        if result.error:
            for error in result.error.split("; "):
                click.echo(f"  - {error}", err=True)
        raise click.ClickException("Registration completed with errors")

    if response.git_tag:
        click.echo(f"Created git tag: {response.git_tag}")

    # Success output
    click.echo(f"Published {len(result.uploaded_files)} file(s) to {destination}")
    for item in result.uploaded_files:
        click.echo(f"  {item['local_path']} -> {item['remote_url']}")
    if result.composites_registered:
        click.echo(f"\nRegistered {len(result.composites_registered)} composite artifact(s):")
        for composite in result.composites_registered:
            root_path = composite.get("root_path", "(unknown)")
            digest = composite.get("hash")
            digest_preview = (
                f"{digest[:12]}..." if isinstance(digest, str) and len(digest) > 12 else digest
            )
            stored = composite.get("component_count_stored")
            total = composite.get("component_count_total")
            component_summary = ""
            if isinstance(stored, int) and isinstance(total, int):
                component_summary = f" ({stored}/{total} components stored)"
            artifact_id = composite.get("artifact_id")
            artifact_suffix = (
                f" id={artifact_id}" if isinstance(artifact_id, str) and artifact_id else ""
            )
            click.echo(f"  {root_path} -> {digest_preview}{component_summary}{artifact_suffix}")
            if composite.get("local_persisted") is False:
                local_error = composite.get("local_error")
                detail = f": {local_error}" if isinstance(local_error, str) and local_error else ""
                click.echo(
                    f"Warning: local composite metadata was not persisted for {root_path}{detail}",
                    err=True,
                )
    click.echo(f"\nJob created: step {result.job_id}")
    if response.git_tag:
        click.echo(f"Git tag: {response.git_tag}")
    # Show GLaaS registration info
    web_url = config_get("glaas.web_url") or "https://glaas.ai"
    session_hash = result.session_hash or ""
    session_url = result.session_url or (f"{web_url}/dag/{session_hash}" if session_hash else "")
    click.echo("\nRegistered with GLaaS:")
    click.echo(f"  View: {session_url}")
