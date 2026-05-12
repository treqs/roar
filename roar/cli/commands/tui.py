"""Launch the interactive terminal UI for browsing roar provenance."""

from __future__ import annotations

import click

from ..context import RoarContext
from ..decorators import require_init


@click.command("tui")
@click.pass_obj
@require_init
def tui(ctx: RoarContext) -> None:
    """Launch the interactive terminal UI.

    Browse the current session's DAG, inspect jobs and artifacts, search
    lineage, view the job log, and launch tracked commands via tmux.
    """
    from ...tui.app import run

    run(roar_dir=ctx.roar_dir, cwd=ctx.cwd)
