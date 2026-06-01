"""
Native Click implementation of the get command.

Usage: roar get <source> [destination] [options]

roar get is LOCAL ONLY — downloads files, hashes them, registers artifacts
locally, and creates a local job record. No GLaaS registration.
"""

from __future__ import annotations

from pathlib import Path

import click

from ...application.get import GetRequest, get_artifacts
from ..context import RoarContext
from ..decorators import require_init


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
@click.option(
    "-n",
    "--name",
    "step_name",
    default=None,
    help="Set the name label for this step.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="For a dataset/prefix, download only the first N data files (subset get).",
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
    step_name: str | None,
    limit: int | None,
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
    # Resolve destination
    dest_str = output_path or destination
    if dest_str is None:
        # Default: current directory with original filename
        dest_str = "."

    dest = Path(dest_str)
    if not dest.is_absolute():
        dest = ctx.cwd / dest

    try:
        response = get_artifacts(
            GetRequest(
                source=source,
                destination=dest,
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                repo_root=ctx.repo_root,
                message=message,
                expected_hash=expected_hash,
                dry_run=dry_run,
                force=force,
                tag=tag,
                step_name=step_name,
                limit=limit,
            )
        )
    except FileExistsError as e:
        raise click.ClickException(str(e)) from e
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except ImportError as e:
        raise click.ClickException(str(e)) from e

    if dry_run:
        click.echo("Dry run - would download:")
        for dry_run_item in response.would_download:
            click.echo(f"  {dry_run_item.remote_url} -> {dry_run_item.local_path}")
        click.echo(f"\nTotal: {len(response.would_download)} file(s)")
        return

    if not response.success:
        raise click.ClickException(response.error or "Download failed")

    if response.git_tag:
        click.echo(f"Created git tag: {response.git_tag}")
    for warning in response.warnings:
        click.echo(f"Warning: {warning}", err=True)

    count = len(response.downloaded_files)
    click.echo(f"Downloaded {count} file(s) from {response.source}")
    for downloaded_file in response.downloaded_files:
        click.echo(f"  {downloaded_file.remote_url} -> {downloaded_file.local_path}")

    if response.job_id is not None:
        click.echo(f"\nJob created: step {response.job_id}")
