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

    # `reproduce` is the on-mission verb but is undiscoverable at the
    # surfaces a user actually visits. Status is one of those surfaces:
    # the user is staring at the active session and may want to know
    # they can rebuild any artifact from it.
    from .._format import hints_should_print, make_hint_printer

    if hints_should_print():
        _caps, hint = make_hint_printer()
        hint("To rebuild an artifact: roar reproduce <artifact-hash>")
        hint("Hashes shown in `roar show <ref>` or `roar dag`.")
