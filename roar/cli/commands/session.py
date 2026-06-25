"""Native Click implementation of the ``session`` command group."""

from __future__ import annotations

import click

from ...application.query.status import StatusQueryError, compute_active_session_hash
from ..context import RoarContext
from ..decorators import require_init


@click.group("session")
def session() -> None:
    """Inspect the active roar session."""


@session.command("hash")
@click.pass_obj
@require_init
def session_hash(ctx: RoarContext) -> None:
    """Print the active session's canonical content hash.

    Prints only the hash (no banner or hints) so it can be substituted directly
    into a shell command — for example a session-scoped S3 key:

    \b
        roar put model.npz s3://$BUCKET/mnist/$(roar session hash)/model.npz
    """
    try:
        click.echo(compute_active_session_hash(ctx.roar_dir))
    except StatusQueryError as exc:
        raise click.ClickException(str(exc)) from exc
