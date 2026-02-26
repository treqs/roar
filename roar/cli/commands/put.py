"""
Native Click implementation of the put command.

Usage: roar put [sources...] <destination> -m "message"

roar put ALWAYS registers lineage with GLaaS. If GLaaS is not configured,
the command will fail.
"""

from __future__ import annotations

import click

from ...config import config_get
from ...core.bootstrap import bootstrap
from ...db.context import create_database_context
from ...services.put import GitError, GitOperations, PutService
from ...services.put.backends import (
    MemoryBackend,
    NoOpBackend,
    parse_destination,
    should_skip_upload,
)
from ...services.transfer import load_backend_class, resolve_backend_for_scheme
from ..context import RoarContext
from ..decorators import require_init


def _get_backend(destination: str):
    """Get the appropriate storage backend for a destination URL.

    If ROAR_PUT_SKIP_UPLOAD=1 is set, returns a NoOpBackend that
    simulates uploads without actual data transfer (for testing).
    """
    parsed = parse_destination(destination)

    # Check for skip-upload mode (for testing)
    if should_skip_upload():
        return NoOpBackend(
            bucket=parsed.bucket,
            prefix=parsed.prefix,
            scheme=parsed.scheme,
        )

    builders = {
        "s3": lambda: load_backend_class(
            "roar.services.put.backends.s3",
            "S3Backend",
            "S3 backend requires boto3. Install with: pip install boto3",
        )(bucket=parsed.bucket, prefix=parsed.prefix),
        "gs": lambda: load_backend_class(
            "roar.services.put.backends.gcs",
            "GCSBackend",
            "GCS backend requires google-cloud-storage. Install with: pip install google-cloud-storage",
        )(bucket=parsed.bucket, prefix=parsed.prefix),
        "memory": lambda: MemoryBackend(bucket=parsed.bucket, prefix=parsed.prefix),
    }

    try:
        return resolve_backend_for_scheme(
            parsed.scheme,
            builders,
            f"Unsupported destination scheme: {parsed.scheme}",
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except ImportError as e:
        raise click.ClickException(str(e)) from e


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
    # Bootstrap DI container (registers logger from config)
    bootstrap(ctx.roar_dir)

    from ...core.logging import get_logger

    logger = get_logger()

    if len(args) < 1:
        raise click.ClickException("Destination URL is required")

    # Last arg is destination, rest are sources
    destination = args[-1]
    sources = list(args[:-1])

    logger.debug(
        "CLI put invoked: sources=%s, destination=%s, message=%r, dry_run=%s, no_tag=%s",
        sources,
        destination,
        message,
        dry_run,
        no_tag,
    )

    # Validate destination is a URL
    try:
        parsed_dest = parse_destination(destination)
        logger.debug(
            "Destination parsed: scheme=%s, bucket=%s, prefix=%s",
            parsed_dest.scheme,
            parsed_dest.bucket,
            parsed_dest.prefix,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    # Get storage backend
    try:
        backend = _get_backend(destination)
        logger.debug("Storage backend: %s", type(backend).__name__)
    except ImportError as e:
        raise click.ClickException(
            f"Missing dependency for {destination}: {e}. Install with: pip install boto3"
        ) from e

    with create_database_context(ctx.roar_dir) as db_ctx:
        # Check for active session
        active_session = db_ctx.sessions.get_active()
        if not active_session:
            raise click.ClickException("No active session. Run 'roar init' first.")

        logger.debug(
            "Active session: id=%s, roar_dir=%s, repo_root=%s",
            active_session["id"],
            ctx.roar_dir,
            ctx.repo_root or ctx.cwd,
        )

        # Create put service
        service = PutService(
            db_context=db_ctx,
            backend=backend,
            destination=destination,
            repo_root=ctx.repo_root or ctx.cwd,
            roar_dir=ctx.roar_dir,
        )

        # Check for dirty git state (unless dry run)
        git_commit = None
        git_tag = None
        expected_tag = None
        if not dry_run:
            try:
                git_ops = GitOperations(repo_root=ctx.repo_root or ctx.cwd)

                # Fail if there are uncommitted changes
                if git_ops.has_uncommitted_changes():
                    logger.debug("Repository has uncommitted changes — aborting")
                    raise click.ClickException(
                        "Repository has uncommitted changes.\n"
                        "Please commit your changes before running 'roar put'.\n"
                        "This ensures artifacts can be traced to a specific commit."
                    )

                git_commit = git_ops.get_current_commit()
                logger.debug("Git commit: %s", git_commit)
                # Compute expected tag name (deterministic)
                if not no_tag:
                    expected_tag = f"roar/{git_commit}"
                    logger.debug("Expected tag: %s", expected_tag)
            except GitError as e:
                logger.debug("Git operation failed: %s", e)
                click.echo(f"Warning: Git operation failed: {e}", err=True)

        # Execute put (include git info in metadata)
        logger.debug("Calling PutService.put()")
        try:
            result = service.put(
                sources=sources,
                message=message,
                dry_run=dry_run,
                git_commit=git_commit,
                git_tag=expected_tag,  # Tag we intend to create
            )
        except FileNotFoundError as e:
            logger.debug("FileNotFoundError: %s", e)
            raise click.ClickException(str(e)) from e
        except ValueError as e:
            logger.debug("ValueError: %s", e)
            raise click.ClickException(str(e)) from e
        except Exception as e:  # pragma: no cover - defensive CLI boundary
            logger.error("Unexpected put failure: %s", e)
            raise click.ClickException(f"Unexpected error during put: {e}") from e

        logger.debug(
            "PutService.put() returned: success=%s, job_id=%s, dry_run=%s, files=%d, error=%s",
            result.success,
            result.job_id,
            result.dry_run,
            len(result.uploaded_files),
            result.error,
        )

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

        # Create git tag after successful upload
        if not no_tag and git_commit:
            try:
                git_ops = GitOperations(repo_root=ctx.repo_root or ctx.cwd)
                git_tag = git_ops.create_tag(git_commit)
                logger.debug("Git tag created: %s", git_tag)
                click.echo(f"Created git tag: {git_tag}")
            except GitError as e:
                logger.debug("Git tag creation failed: %s", e)
                click.echo(f"Warning: Could not create git tag: {e}", err=True)

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
                    detail = (
                        f": {local_error}" if isinstance(local_error, str) and local_error else ""
                    )
                    click.echo(
                        f"Warning: local composite metadata was not persisted for {root_path}{detail}",
                        err=True,
                    )
        click.echo(f"\nJob created: step {result.job_id}")
        if git_tag:
            click.echo(f"Git tag: {git_tag}")
        # Show GLaaS registration info
        web_url = config_get("glaas.web_url") or "https://glaas.ai"
        session_hash = result.session_hash or ""
        session_url = result.session_url or (
            f"{web_url}/dag/{session_hash}" if session_hash else ""
        )
        click.echo("\nRegistered with GLaaS:")
        click.echo(f"  View: {session_url}")
        logger.debug("Put command completed successfully")
