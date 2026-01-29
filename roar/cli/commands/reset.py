"""
Native Click implementation of the reset command.

Usage: roar reset
"""

from __future__ import annotations

import click

from ...db.context import create_database_context
from ..context import RoarContext
from ..decorators import require_init


@click.command("reset")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt.",
)
@click.pass_obj
@require_init
def reset(ctx: RoarContext, yes: bool) -> None:
    """Start a fresh session.

    Deactivates the current active session and creates a new one.
    The previous session data is preserved in the database.

    \b
    Examples:

        roar reset              # Reset with confirmation prompt

        roar reset -y           # Reset without confirmation
    """
    with create_database_context(ctx.roar_dir) as db_ctx:
        active_session = db_ctx.sessions.get_active()

        if active_session:
            old_session_id = active_session["id"]
            steps = db_ctx.sessions.get_steps(old_session_id)
            step_count = len(steps)

            click.echo(f"Current session has {step_count} step(s).")

            if not yes and not click.confirm("Start a new session?", default=True):
                click.echo("Aborted.")
                return

            new_session_id = db_ctx.sessions.create(make_active=True)
            click.echo(f"Deactivated session {old_session_id}.")
            click.echo(f"Created new session {new_session_id}.")
        else:
            if not yes and not click.confirm("No active session. Create one?", default=True):
                click.echo("Aborted.")
                return

            new_session_id = db_ctx.sessions.create(make_active=True)
            click.echo(f"Created new session {new_session_id}.")
