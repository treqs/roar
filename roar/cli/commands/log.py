"""Native Click wrapper for the local log query."""

from __future__ import annotations

import sys

import click

from ...application.query import LogQueryRequest, render_log
from ..context import RoarContext
from ..decorators import require_init


@click.command("log")
@click.pass_obj
@require_init
def log(ctx: RoarContext) -> None:
    """Display recent job execution history.

    Shows the 20 most recent jobs in a table format with their
    UID, step reference, timestamp, duration, status, and command.

    \b
    Examples:

        roar log              # Show recent job history
    """
    click.echo(render_log(LogQueryRequest(roar_dir=ctx.roar_dir, use_color=sys.stdout.isatty())))
