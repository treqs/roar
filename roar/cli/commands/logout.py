from __future__ import annotations

from contextlib import suppress

import click

from ...auth_store import auth_store_path, clear_auth_state, load_auth_state
from ...glaas_auth import resolve_auth_api_url, revoke_token


@click.command("logout")
def logout() -> None:
    """Clear the global GLaaS auth state."""
    path = auth_store_path()
    auth_state = load_auth_state()
    if auth_state and auth_state.access_token:
        with suppress(Exception):
            revoke_token(resolve_auth_api_url(), auth_state.access_token)

    removed = clear_auth_state()
    if removed:
        click.echo(f"Logged out and removed {path}")
    else:
        click.echo(f"No auth state found at {path}")
