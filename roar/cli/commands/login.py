from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from ...auth_store import (
    auth_state_from_dict,
    is_auth_state_expired,
    load_auth_state,
    save_auth_state,
)
from ...glaas_auth import (
    GlaasAuthError,
    build_auth_state_from_device_login,
    fetch_access_context_via_auth_api,
    open_browser,
    poll_device_token,
    resolve_auth_api_url,
    start_device_authorization,
)


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
    hidden=True,
    help="Bootstrap a development auth state using a local dev bearer token for the given email.",
)
@click.option(
    "--glaas-api-url",
    "auth_api_url",
    required=False,
    help="Override the GLaaS API base URL used for browser and device login.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Replace any existing session without prompting.",
)
def login(
    token_file: Path | None, dev_email: str | None, auth_api_url: str | None, force: bool
) -> None:
    """Store global GLaaS auth state."""
    if token_file is not None and dev_email is not None:
        raise click.ClickException("Provide at most one of --token-file or --dev-email")
    if dev_email is not None:
        _require_dev_email_feature_flag()

    resolved_api_url = resolve_auth_api_url(auth_api_url)
    _confirm_session_replacement(force=force)

    if token_file is not None:
        raw_data = _load_auth_state_from_token_file(token_file, glaas_api_url=resolved_api_url)
    elif dev_email is not None:
        raw_data = build_dev_auth_state(dev_email)
    else:
        raw_data = _run_device_login(resolved_api_url)

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


def _run_device_login(resolved_api_url: str) -> dict[str, object]:
    try:
        session = start_device_authorization(resolved_api_url)
    except GlaasAuthError as exc:
        raise click.ClickException(f"Failed to start GLaaS device login: {exc}") from exc

    verification_url = session.verification_uri
    click.echo("Starting GLaaS device login...")
    click.echo(f"Open this URL to approve login: {verification_url}")
    click.echo(f"Enter this code on the site: {session.user_code}")

    if open_browser(verification_url):
        click.echo("Opened browser to the GLaaS approval page. Enter the code shown above.")
    else:
        click.echo(
            "Could not open a browser automatically. Open the URL above manually and enter the code shown above."
        )

    click.echo("Waiting for approval...")
    try:
        token_response = poll_device_token(resolved_api_url, session)
        access_context = fetch_access_context_via_auth_api(
            resolved_api_url, access_token=token_response.access_token
        )
    except GlaasAuthError as exc:
        raise click.ClickException(str(exc)) from exc

    return build_auth_state_from_device_login(token_response, access_context)


def _load_auth_state_from_token_file(token_file: Path, *, glaas_api_url: str) -> dict[str, object]:
    try:
        raw_data = json.loads(token_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Token file {token_file} is not valid JSON") from exc
    if not isinstance(raw_data, dict):
        raise click.ClickException(f"Token file {token_file} must contain a JSON object")

    access_token = raw_data.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return raw_data

    try:
        access_context = fetch_access_context_via_auth_api(glaas_api_url, access_token=access_token)
    except GlaasAuthError as exc:
        raise click.ClickException(
            f"Token file contains an invalid or expired token: {exc}"
        ) from exc

    user = access_context.get("user")
    if isinstance(user, dict):
        raw_user = raw_data.get("user")
        if not isinstance(raw_user, dict):
            raw_user = {}
        raw_data["user"] = {
            **raw_user,
            "sub": user.get("sub") or raw_user.get("sub"),
            "db_user_id": user.get("id") or raw_user.get("db_user_id"),
            "email": user.get("email") or raw_user.get("email"),
            "username": user.get("username") or raw_user.get("username"),
        }
    return raw_data


def _confirm_session_replacement(*, force: bool) -> None:
    if force:
        return

    existing = load_auth_state()
    if existing is None or is_auth_state_expired(existing):
        return

    identity = existing.user.username or existing.user.email or existing.user.sub or "unknown"
    click.echo(f"Already logged in as {identity}.")
    if not click.confirm("Replace existing session?", default=False):
        raise click.ClickException("Login cancelled; existing session preserved.")


def _require_dev_email_feature_flag() -> None:
    enabled = os.environ.get("ROAR_ENABLE_DEV_EMAIL_LOGIN", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not enabled:
        raise click.ClickException(
            "--dev-email is disabled by default. Set ROAR_ENABLE_DEV_EMAIL_LOGIN=1 for local development only."
        )


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
