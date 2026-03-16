"""Native Click wrapper for the local show query."""

from __future__ import annotations

import click

from ...application.query import ShowQueryRequest, render_show
from ..context import RoarContext
from ..decorators import require_init


@click.command("show")
@click.argument("ref", required=False)
@click.pass_obj
@require_init
def show(ctx: RoarContext, ref: str | None) -> None:
    """Show session, job, or artifact details.

    Without arguments, displays the active session and its jobs.
    With a reference, displays detailed information based on the reference type.

    \b
    REF can be:
      - @N or @BN: Job by step number (e.g., @1, @B2)
      - 8-char hex: Job by UID
      - Longer hex: Artifact by hash (falls back to job if found)
      - File path: Artifact at that path (e.g., ./output/model.pkl)

    \b
    Examples:
        roar show                          # Show active session overview
        roar show @1                       # Show details for step 1
        roar show @B1                      # Show details for build step 1
        roar show a1b2c3d4                 # Show job by UID
        roar show a1b2c3d4e5f67890...      # Show artifact by hash
        roar show ./output/model.pkl       # Show artifact by path
    """
    click.echo(render_show(ShowQueryRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, ref=ref)))
