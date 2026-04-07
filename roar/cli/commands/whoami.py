from __future__ import annotations

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

from datetime import datetime, timezone
from typing import Any

import click

from ...auth_store import auth_store_path, load_auth_state, parse_auth_state_expiry
from ...integrations.config.loader import find_config_file


@click.command("whoami")
def whoami() -> None:
    """Show the current GLaaS/TReqs login state and repo binding."""
    auth_state = load_auth_state()
    if auth_state is None or not auth_state.access_token:
        raise click.ClickException(
            "Not logged in. Run `roar login` once global auth support is configured."
        )

    identity = auth_state.user.username or auth_state.user.email or auth_state.user.sub or "unknown"
    email = auth_state.user.email
    if email:
        click.echo(f"Authenticated as {identity} <{email}>")
    else:
        click.echo(f"Authenticated as {identity}")

    click.echo(f"Provider: {auth_state.provider}")
    if auth_state.user.db_user_id:
        click.echo(f"TReqs user id: {auth_state.user.db_user_id}")
    click.echo(f"Auth store: {auth_store_path()}")
    if auth_state.expires_at:
        click.echo(f"Token expires at: {auth_state.expires_at}")
        for status_line in _build_expiry_status_lines(auth_state.expires_at, has_refresh_token=bool(auth_state.refresh_token)):
            click.echo(status_line)

    binding = _load_repo_binding()
    if binding is None:
        click.echo("Repo binding: none")
        return

    owner_type = binding.get("owner_type") or "unknown"
    owner_id = binding.get("owner_id") or "unknown"
    project_id = binding.get("project_id")
    if project_id:
        click.echo(f"Repo binding: {owner_type} {owner_id} / project {project_id}")
    else:
        click.echo(f"Repo binding: {owner_type} {owner_id}")


def _load_repo_binding() -> dict[str, Any] | None:
    config_path = find_config_file()
    if config_path is None or config_path.name != "config.toml":
        return None

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    treqs = data.get("treqs")
    if not isinstance(treqs, dict):
        return None
    return treqs


def _build_expiry_status_lines(expires_at: str, *, has_refresh_token: bool) -> list[str]:
    parsed_expiry = parse_auth_state_expiry(expires_at)
    if parsed_expiry is None:
        return []

    now = datetime.now(timezone.utc)
    remaining_seconds = int((parsed_expiry - now).total_seconds())

    if remaining_seconds <= 0:
        if has_refresh_token:
            return ["Session status: expired (refresh token available)"]
        return ["Session status: expired (run `roar login`)"]

    if remaining_seconds <= 5 * 60:
        minutes = max(1, remaining_seconds // 60)
        if has_refresh_token:
            return [f"Session status: expiring soon (~{minutes}m remaining; refresh token available)"]
        return [f"Session status: expiring soon (~{minutes}m remaining; no refresh token stored)"]

    if has_refresh_token:
        return ["Session status: valid (refresh token available)"]
    return ["Session status: valid"]
