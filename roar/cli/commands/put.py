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
from ..context import RoarContext
from ..decorators import require_init
from ..publish_intent import (
    confirm_anonymous_public_publish,
    resolve_publish_intent,
    warn_public_default,
)


def _preview_hash(value: str | None) -> str | None:
    """Shorten long hashes for CLI summaries."""
    if not value:
        return None
    return f"{value[:12]}..." if len(value) > 12 else value


def _resolve_glaas_web_url() -> str:
    """Load the GLaaS web URL lazily for success output."""
    from ...integrations.config import config_get

    return config_get("glaas.web_url") or "https://glaas.ai"


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
@click.option(
    "--public/--private",
    "public",
    default=None,
    help=(
        "Submit as public or private lineage. When omitted, roar uses "
        "registration.public_by_default from config. Public allows anonymous or "
        "attributed submission; private requires attributed submission."
    ),
)
@click.option(
    "--anonymous",
    is_flag=True,
    help="Force public anonymous registration even when local GLaaS auth is configured.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt for anonymous public publishing.",
)
@click.option("-n", "--name", "step_name", help="Set the name label for this step.")
@click.option(
    "--dataset",
    "as_dataset",
    is_flag=True,
    help="Declare each source directory a dataset (composite) even if its structure "
    "isn't auto-detected. A composite still requires >=2 files.",
)
@click.pass_obj
@require_init
def put(
    ctx: RoarContext,
    args: tuple[str, ...],
    message: str,
    dry_run: bool,
    no_tag: bool,
    public: bool | None,
    anonymous: bool,
    yes: bool,
    step_name: str | None,
    as_dataset: bool,
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

    Visibility / attribution matrix:
    - effective private -> private + attributed only
    - effective public -> public + anonymous OR public + attributed
    - --anonymous -> public + anonymous, ignoring configured auth
    - private + anonymous is not allowed

    Effective visibility comes from `--public` / `--private` when provided,
    otherwise from this repo's `roar scope` setting. `--anonymous` forces
    public visibility. If the effective scope is anonymous, roar prompts before
    publishing public anonymous lineage unless --yes is provided.

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

    if anonymous and public is False:
        raise click.ClickException("--anonymous requires public visibility; remove --private.")

    publish_intent = resolve_publish_intent(
        public,
        anonymous,
        start_dir=str(ctx.repo_root or ctx.cwd),
    )
    if publish_intent.used_public_default:
        warn_public_default()

    if (
        publish_intent.anonymous
        and not yes
        and not dry_run
        and not confirm_anonymous_public_publish(
            command_name="roar put", start_dir=str(ctx.repo_root or ctx.cwd)
        )
    ):
        click.echo("Publication aborted.")
        raise SystemExit(1)

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
                public=publish_intent.public,
                anonymous=publish_intent.anonymous,
                no_tag=no_tag,
                step_name=step_name,
                as_dataset=as_dataset,
            )
        )
    except (FileNotFoundError, ValueError, ImportError) as e:
        raise click.ClickException(str(e)) from e
    except Exception as e:  # pragma: no cover - defensive CLI boundary
        raise click.ClickException(f"Unexpected error during put: {e}") from e

    for warning in response.warnings:
        click.echo(f"Warning: {warning}", err=True)

    # Handle dry run output
    if response.dry_run:
        click.echo(
            f"Dry run: would upload {len(response.would_upload)} file(s) to {response.destination}"
        )
        for dry_run_item in response.would_upload:
            click.echo(f"  {dry_run_item.path}")
        return

    # Check for registration errors
    if not response.success:
        click.echo(f"Published {len(response.uploaded_files)} file(s) to {response.destination}")
        if response.job_uid:
            click.echo(f"Local details: roar show --job {response.job_uid}")
        click.echo("\nWarning: Registration completed with errors:", err=True)
        if response.error:
            for error in response.error.split("; "):
                click.echo(f"  - {error}", err=True)
        raise click.ClickException("Registration completed with errors")

    # Success output
    click.echo(f"Published {len(response.uploaded_files)} file(s) to {response.destination}")
    session_preview = _preview_hash(response.session_hash)
    if session_preview:
        click.echo(f"Session: {session_preview}")
    if response.job_id is not None:
        click.echo(f"Job step: @{response.job_id}")
    if response.job_uid:
        click.echo(f"Job UID: {response.job_uid}")
    if response.git_tag:
        click.echo(f"Git tag: {response.git_tag}")
    if response.uploaded_files:
        click.echo("")
        click.echo("Uploaded files:")
    for uploaded_file in response.uploaded_files:
        click.echo(f"  {uploaded_file.local_path} -> {uploaded_file.remote_url}")
    if response.composites_registered:
        click.echo(f"\nRegistered {len(response.composites_registered)} composite artifact(s):")
        for composite in response.composites_registered:
            root_path = composite.root_path or "(unknown)"
            digest = composite.hash
            digest_preview = (
                f"{digest[:12]}..." if isinstance(digest, str) and len(digest) > 12 else digest
            )
            stored = composite.component_count_stored
            total = composite.component_count_total
            component_summary = ""
            if isinstance(stored, int) and isinstance(total, int):
                component_summary = f" ({stored}/{total} components stored)"
            artifact_id = composite.artifact_id
            artifact_suffix = (
                f" id={artifact_id}" if isinstance(artifact_id, str) and artifact_id else ""
            )
            click.echo(f"  {root_path} -> {digest_preview}{component_summary}{artifact_suffix}")
            if composite.local_persisted is False:
                local_error = composite.local_error
                detail = f": {local_error}" if isinstance(local_error, str) and local_error else ""
                click.echo(
                    f"Warning: local composite metadata was not persisted for {root_path}{detail}",
                    err=True,
                )

    web_url = _resolve_glaas_web_url()
    session_hash = response.session_hash or ""
    session_url = response.session_url or (f"{web_url}/dag/{session_hash}" if session_hash else "")
    if session_url:
        click.echo("\nGLaaS:")
        click.echo(f"  Session: {session_url}")

    click.echo("\nNext:")
    if response.job_uid:
        click.echo(f"  roar show --job {response.job_uid}")
    click.echo("  roar show --session")
