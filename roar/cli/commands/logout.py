from __future__ import annotations

import click

from ...auth_store import auth_store_path, clear_auth_state


@click.command("logout")
def logout() -> None:
    """Clear the global treqs auth state."""
    path = auth_store_path()
    removed = clear_auth_state()
    if removed:
        click.echo(f"Logged out and removed {path}")
    else:
        click.echo(f"No auth state found at {path}")
