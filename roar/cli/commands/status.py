"""Native Click wrapper for the local status query."""

from __future__ import annotations

import click

from ...application.query import StatusQueryRequest, render_status
from ..context import RoarContext
from ..decorators import require_init


@click.command("status")
@click.pass_obj
@require_init
def status(ctx: RoarContext) -> None:
    """Show a summary of the active session."""
    click.echo(render_status(StatusQueryRequest(roar_dir=ctx.roar_dir)))
