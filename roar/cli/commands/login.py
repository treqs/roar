from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from ...auth_store import auth_state_from_dict, save_auth_state


@click.command("login")
@click.option(
    "--token-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=False,
    help="Import an auth state JSON file into the global roar auth store.",
)
@click.option(
    "--dev-email",
    required=False,
    help="Bootstrap a development auth state using a local dev bearer token for the given email.",
)
def login(token_file: Path | None, dev_email: str | None) -> None:
    """Store global treqs auth state.

    This bootstrap flow is intended for local development until the browser/device
    login flow is implemented.
    """
    if bool(token_file) == bool(dev_email):
        raise click.ClickException("Provide exactly one of --token-file or --dev-email")

    if token_file is not None:
        try:
            raw_data = json.loads(token_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Token file {token_file} is not valid JSON") from exc
        if not isinstance(raw_data, dict):
            raise click.ClickException(f"Token file {token_file} must contain a JSON object")
    else:
        raw_data = build_dev_auth_state(dev_email or '')

    auth_state = auth_state_from_dict(raw_data)
    if not auth_state.access_token:
        raise click.ClickException("Auth state is missing access_token")

    path = save_auth_state(raw_data)
    identity = auth_state.user.username or auth_state.user.email or auth_state.user.sub or "unknown"
    email = auth_state.user.email
    if email:
        click.echo(f"Stored auth state for {identity} <{email}>")
    else:
        click.echo(f"Stored auth state for {identity}")
    click.echo(f"Saved to {path}")


def build_dev_auth_state(dev_email: str) -> dict[str, object]:
    email = dev_email.strip()
    if not email:
        raise click.ClickException("--dev-email requires a non-empty email address")

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    return {
        "version": 1,
        "provider": "treqs-dev",
        "issuer": "dev-local-auth",
        "client_id": "dev-local-client",
        "access_token": f"dev-email:{email}",
        "refresh_token": None,
        "id_token": None,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "user": {
            "sub": f"dev:{email}",
            "db_user_id": None,
            "email": email,
            "username": email,
        },
    }
