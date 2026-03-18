"""Native Click wrapper for the local status query."""

from __future__ import annotations

import click

from ...application.query.requests import StatusQueryRequest
from ...application.query.status import StatusQueryError, render_status
from ..context import RoarContext
from ..decorators import require_init


@click.command("status")
@click.pass_obj
@require_init
def status(ctx: RoarContext) -> None:
    """Show a summary of the active session."""
    try:
        click.echo(render_status(StatusQueryRequest(roar_dir=ctx.roar_dir)))
    except StatusQueryError as exc:
        raise click.ClickException(str(exc)) from exc
