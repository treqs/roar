"""Native Click wrapper for the local database status query."""

from __future__ import annotations

import click

from ...application.query.db_status import DbStatusQueryError, render_db_status
from ...application.query.requests import DbStatusQueryRequest
from ..context import RoarContext
from ..decorators import require_init


@click.command("db")
@click.pass_obj
@require_init
def db(ctx: RoarContext) -> None:
    """Show local database status: size, contents, sync state, hygiene, and age.

    A view of the SQLite database itself — distinct from `roar status`, which
    summarizes the active session.
    """
    from .._format import hints_should_print, make_hint_printer, print_brand_header

    print_brand_header("db")
    if hints_should_print():
        click.echo()
    try:
        click.echo(render_db_status(DbStatusQueryRequest(roar_dir=ctx.roar_dir)))
    except DbStatusQueryError as exc:
        raise click.ClickException(str(exc)) from exc

    if hints_should_print():
        _caps, hint = make_hint_printer()
        hint("Orphaned/superseded rows are reclaimable — `roar gc` (coming soon).")
