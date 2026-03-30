from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click


@dataclass(frozen=True)
class AuthenticatedUser:
    sub: str
    db_user_id: str | None
    email: str | None
    username: str | None


@dataclass(frozen=True)
class AuthState:
    version: int
    provider: str
    issuer: str | None
    client_id: str | None
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_at: str | None
    user: AuthenticatedUser


def auth_store_path() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "roar" / "auth.json"
    return Path.home() / ".config" / "roar" / "auth.json"


def load_auth_state() -> AuthState | None:
    path = auth_store_path()
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Stored auth state at {path} is not valid JSON") from exc

    if not isinstance(data, dict):
        raise click.ClickException(f"Stored auth state at {path} must be a JSON object")

    return auth_state_from_dict(data)


def auth_state_from_dict(data: dict[str, Any]) -> AuthState:
    user = data.get("user") or {}
    if not isinstance(user, dict):
        raise click.ClickException("Auth state field 'user' must be a JSON object")

    return AuthState(
        version=int(data.get("version", 1)),
        provider=str(data.get("provider", "unknown")),
        issuer=_optional_string(data.get("issuer")),
        client_id=_optional_string(data.get("client_id")),
        access_token=_required_string(data.get("access_token"), field_name="access_token"),
        refresh_token=_optional_string(data.get("refresh_token")),
        id_token=_optional_string(data.get("id_token")),
        expires_at=_optional_string(data.get("expires_at")),
        user=AuthenticatedUser(
            sub=_optional_string(user.get("sub")) or "",
            db_user_id=_optional_string(user.get("db_user_id")),
            email=_optional_string(user.get("email")),
            username=_optional_string(user.get("username")),
        ),
    )


def save_auth_state(raw_data: dict[str, Any]) -> Path:
    path = auth_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw_data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def clear_auth_state() -> bool:
    path = auth_store_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        return stripped
    return str(value)


def _required_string(value: Any, *, field_name: str) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise click.ClickException(f"Auth state is missing {field_name}")
    return normalized
