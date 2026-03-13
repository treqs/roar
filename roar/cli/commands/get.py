"""
Native Click implementation of the get command.

Usage: roar get <source> [destination] [options]

roar get is LOCAL ONLY — downloads files, hashes them, registers artifacts
locally, and creates a local job record. No GLaaS registration.
"""

from __future__ import annotations

from pathlib import Path

import click

from ...core.bootstrap import bootstrap
from ...db.context import create_database_context
from ...services.get.backends import NoOpDownloadBackend, parse_source, should_skip_download
from ...services.transfer import load_backend_class, resolve_backend_for_scheme
from ..context import RoarContext
from ..decorators import require_init


def _get_backend(source_url: str):
    """Get the appropriate download backend for a source URL.

    If ROAR_GET_SKIP_DOWNLOAD=1 is set, returns a NoOpDownloadBackend that
    simulates downloads without actual data transfer (for testing).
    """
    parsed = parse_source(source_url)

    # Check for skip-download mode (for testing)
    if should_skip_download():
        return NoOpDownloadBackend(
            bucket=parsed.bucket,
            scheme=parsed.scheme,
        )

    from ...services.get.backends.http import HTTPBackend

    builders = {
        "http": lambda: HTTPBackend(url=source_url),
        "https": lambda: HTTPBackend(url=source_url),
        "s3": lambda: load_backend_class(
            "roar.services.get.backends.s3",
            "S3DownloadBackend",
            "S3 backend requires boto3. Install with: pip install boto3",
        )(bucket=parsed.bucket),
        "gs": lambda: load_backend_class(
            "roar.services.get.backends.gcs",
            "GCSDownloadBackend",
            "GCS backend requires google-cloud-storage. Install with: pip install google-cloud-storage",
        )(bucket=parsed.bucket),
    }

    try:
        return resolve_backend_for_scheme(
            parsed.scheme,
            builders,
            f"Unsupported source scheme: {parsed.scheme}",
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except ImportError as e:
        raise click.ClickException(str(e)) from e


@click.command("get")
@click.argument("source")
@click.argument("destination", required=False, default=None)
@click.option(
    "-m",
    "--message",
    default=None,
    help="Optional annotation message.",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    default=None,
    help="Output path (alternative to positional destination).",
)
@click.option(
    "--hash",
    "expected_hash",
    default=None,
    help="Expected BLAKE3 hash (fails if downloaded content doesn't match).",
)
@click.option(
    "--tag",
    is_flag=True,
    help="Create git tag after download (format: roar/<commit>).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing files.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be downloaded without doing it.",
)
@click.pass_obj
@require_init
def get(
    ctx: RoarContext,
    source: str,
    destination: str | None,
    message: str | None,
    output_path: str | None,
    expected_hash: str | None,
    tag: bool,
    force: bool,
    dry_run: bool,
) -> None:
    """Download artifacts from cloud storage and record in the local DAG.

    This is the inverse of 'roar put'. It downloads files, hashes them,
    registers artifacts locally, and creates a local job record with
    job_type="get". The downloaded artifacts become SOURCE nodes in the DAG.

    Downloaded artifacts will appear in GLaaS naturally when downstream
    jobs consume them and get registered via 'roar put' or 'roar register'.

    \b
    Source formats:
        https://example.com/file.pt    HTTP(S) download
        s3://bucket/path/file.pt       AWS S3
        gs://bucket/path/file.pt       Google Cloud Storage
        s3://bucket/prefix/            S3 prefix (directory download)

    \b
    Examples:

        # Download single file from HTTPS
        roar get https://huggingface.co/model/weights.bin

        # Download from S3
        roar get s3://my-bucket/models/model.pt

        # Download to specific path
        roar get s3://bucket/model.pt ./models/

        # Download entire S3 prefix
        roar get s3://my-bucket/checkpoints/ ./checkpoints/

        # Download and verify hash
        roar get s3://bucket/model.pt --hash abc123...

        # Dry run to see what would be downloaded
        roar get s3://bucket/data/ ./data/ --dry-run
    """
    # Bootstrap DI container
    bootstrap(ctx.roar_dir)

    from ...core.logging import get_logger

    logger = get_logger()

    # Resolve destination
    dest_str = output_path or destination
    if dest_str is None:
        # Default: current directory with original filename
        dest_str = "."

    dest = Path(dest_str)
    if not dest.is_absolute():
        dest = ctx.cwd / dest

    logger.debug(
        "CLI get invoked: source=%s, destination=%s, message=%r, dry_run=%s, tag=%s, force=%s",
        source,
        dest,
        message,
        dry_run,
        tag,
        force,
    )

    # Parse source URL
    try:
        parsed_source = parse_source(source)
        logger.debug(
            "Source parsed: scheme=%s, bucket=%s, key=%s",
            parsed_source.scheme,
            parsed_source.bucket,
            parsed_source.key,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    # Detect prefix download (source ends with /)
    is_prefix = source.rstrip("/") != source or parsed_source.is_prefix

    # Get download backend
    try:
        backend = _get_backend(source)
        logger.debug("Download backend: %s", type(backend).__name__)
    except ImportError as e:
        raise click.ClickException(f"Missing dependency for {source}: {e}") from e

    # Git operations (optional for get)
    git_commit = None
    git_tag_name = None
    if not dry_run:
        try:
            from ...application.publish.git import resolve_publish_git_state

            git_commit = resolve_publish_git_state(ctx.repo_root or ctx.cwd).commit
            logger.debug("Git commit: %s", git_commit)
        except Exception as e:
            logger.debug("Git operation failed (non-fatal for get): %s", e)

    with create_database_context(ctx.roar_dir) as db_ctx:
        # Check for active session (unless dry-run)
        if not dry_run:
            active_session = db_ctx.sessions.get_active()
            if not active_session:
                raise click.ClickException("No active session. Run 'roar init' first.")

        from ...services.get.service import GetService

        service = GetService(
            db_context=db_ctx,
            backend=backend,
            source=parsed_source,
            repo_root=ctx.repo_root or ctx.cwd,
        )

        try:
            result = service.get(
                destination=dest,
                message=message,
                expected_hash=expected_hash,
                dry_run=dry_run,
                force=force,
                git_commit=git_commit,
                is_prefix=is_prefix,
            )
        except FileExistsError as e:
            raise click.ClickException(str(e)) from e
        except FileNotFoundError as e:
            raise click.ClickException(str(e)) from e
        except ValueError as e:
            raise click.ClickException(str(e)) from e

        logger.debug(
            "GetService.get() returned: success=%s, job_id=%s, dry_run=%s, files=%d, error=%s",
            result.success,
            result.job_id,
            result.dry_run,
            len(result.downloaded_files),
            result.error,
        )

        # Handle dry run output
        if dry_run:
            click.echo("Dry run - would download:")
            for item in result.would_download:
                click.echo(f"  {item['remote_url']} -> {item['local_path']}")
            click.echo(f"\nTotal: {len(result.would_download)} file(s)")
            return

        # Handle errors
        if not result.success:
            raise click.ClickException(result.error or "Download failed")

        # Create git tag after successful download (opt-in)
        if tag and git_commit:
            try:
                from ...application.publish.git import (
                    build_publish_tag_name,
                    create_publish_git_tag,
                )

                git_tag_name = build_publish_tag_name(git_commit)
                success, tag_error = create_publish_git_tag(ctx.repo_root or ctx.cwd, git_tag_name)
                if success:
                    logger.debug("Git tag created: %s", git_tag_name)
                    click.echo(f"Created git tag: {git_tag_name}")
                elif tag_error:
                    raise ValueError(tag_error)
            except Exception as e:
                logger.debug("Git tag creation failed: %s", e)
                click.echo(f"Warning: Could not create git tag: {e}", err=True)

        # Success output
        count = len(result.downloaded_files)
        click.echo(f"Downloaded {count} file(s) from {source}")
        for item in result.downloaded_files:
            click.echo(f"  {item['remote_url']} -> {item['local_path']}")

        if result.job_id is not None:
            click.echo(f"\nJob created: step {result.job_id}")

        logger.debug("Get command completed successfully")
