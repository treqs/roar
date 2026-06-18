"""Native Click wrapper for the local status query."""

from __future__ import annotations

import click

from ...application.query.requests import StatusQueryRequest
from ...application.query.status import StatusQueryError, render_status, render_untracked_dirs
from ..context import RoarContext
from ..decorators import require_init


@click.command("status")
@click.option(
    "--untracked-dirs",
    is_flag=True,
    help="List output artifacts whose directory won't exist on a clean checkout "
    "(untracked in-repo dirs, or paths outside the repo).",
)
@click.pass_obj
@require_init
def status(ctx: RoarContext, untracked_dirs: bool) -> None:
    """Show a summary of the active session."""
    from .._format import hints_should_print, make_hint_printer, print_brand_header

    if untracked_dirs:
        try:
            click.echo(render_untracked_dirs(StatusQueryRequest(roar_dir=ctx.roar_dir), ctx.cwd))
        except StatusQueryError as exc:
            raise click.ClickException(str(exc)) from exc
        return

    # The banner and the trailing hints both check `hints_should_print()`
    # internally, so non-TTY / quiet contexts get clean output.
    print_brand_header("status")
    if hints_should_print():
        click.echo()
    try:
        click.echo(render_status(StatusQueryRequest(roar_dir=ctx.roar_dir)))
    except StatusQueryError as exc:
        raise click.ClickException(str(exc)) from exc

    # `reproduce` is the on-mission verb but is undiscoverable at the
    # surfaces a user actually visits. Status is one of those surfaces:
    # the user is staring at the active session and may want to know
    # they can rebuild any artifact from it.
    if hints_should_print():
        _caps, hint = make_hint_printer()
        hint("To rebuild an artifact: roar reproduce <artifact-hash>")
        hint("Hashes shown in `roar show <ref>` or `roar dag`.")
