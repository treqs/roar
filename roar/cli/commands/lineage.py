"""Native Click wrapper for the local lineage query."""

import click

from ...application.query.lineage import render_lineage
from ...application.query.requests import LineageQueryRequest
from ..context import RoarContext
from ..decorators import require_init


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
    try:
        rendered = render_lineage(
            LineageQueryRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                artifact=artifact,
                output=output,
                depth=depth,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)
