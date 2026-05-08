"""CLI command for exporting an offline Roar registration package."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ...application.publish.registration_package import (
    ExportRegistrationPackageRequest,
)
from ...application.publish.registration_package import (
    export_registration_package as export_registration_package_app,
)
from ..context import RoarContext
from ..decorators import require_init


@click.command("export-registration-package")
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to write the Roar registration package.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Write a machine-readable result to stdout.",
)
@click.pass_obj
@require_init
def export_registration_package(
    ctx: RoarContext,
    output_path: Path,
    json_output: bool,
) -> None:
    """Export the current local session as a Roar registration package."""
    response = export_registration_package_app(
        ExportRegistrationPackageRequest(
            output=output_path,
            roar_dir=ctx.roar_dir,
            cwd=ctx.cwd,
        )
    )

    if not response.success:
        if json_output:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": response.error or "Registration package export failed",
                    },
                    sort_keys=True,
                )
            )
            raise SystemExit(1)
        raise click.ClickException(response.error or "Registration package export failed")

    payload = {
        "success": True,
        "package_path": response.package_path,
        "package_sha256": response.package_sha256,
        "job_count": response.job_count,
        "artifact_count": response.artifact_count,
        "link_count": response.link_count,
    }
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True))
        return

    click.echo(f"Exported registration package: {response.package_path}")
    click.echo(f"  SHA256: {response.package_sha256}")
    click.echo(f"  Jobs: {response.job_count}")
    click.echo(f"  Artifacts: {response.artifact_count}")
    click.echo(f"  Links: {response.link_count}")
