from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .auth_store import auth_store_path, is_auth_state_expired, load_auth_state, save_auth_state
from .glaas_auth import (
    GlaasAuthError,
    fetch_access_context_via_auth_api,
    refresh_access_token,
    resolve_auth_api_url,
)


class GlaasClientError(RuntimeError):
    pass


def fetch_access_context(glaas_api_url: str, access_token: str | None = None) -> dict[str, Any]:
    resolved_access_token = _resolve_access_token(access_token)
    try:
        return fetch_access_context_via_auth_api(glaas_api_url, access_token=resolved_access_token)
    except GlaasAuthError as exc:
        raise GlaasClientError(f"Failed to fetch GLaaS auth access context: {exc}") from exc


def fetch_user_projects(
    glaas_api_url: str, access_token: str | None = None
) -> list[dict[str, Any]]:
    access_context = fetch_access_context(glaas_api_url, access_token=access_token)

    projects_by_owner = access_context.get("projects_by_owner")
    if not isinstance(projects_by_owner, dict):
        raise GlaasClientError(
            "Invalid GLaaS auth access-context payload: projects_by_owner missing"
        )

    projects: list[dict[str, Any]] = []
    for owner_projects in projects_by_owner.values():
        if not isinstance(owner_projects, list):
            raise GlaasClientError(
                "Invalid GLaaS auth access-context payload: owner projects invalid"
            )
        for project in owner_projects:
            if not isinstance(project, dict):
                raise GlaasClientError(
                    "Invalid GLaaS auth access-context payload: owner projects invalid"
                )
            projects.append(project)
    return projects


def _resolve_access_token(access_token: str | None) -> str:
    normalized_access_token = (access_token or "").strip()
    if normalized_access_token:
        return normalized_access_token

    auth_state = load_auth_state()
    if auth_state is None or not auth_state.access_token:
        raise GlaasClientError("Not logged in. Run `roar login` before linking a project.")

    if _should_refresh(auth_state):
        return _refresh_stored_access_token(auth_state)
    if is_auth_state_expired(auth_state):
        raise GlaasClientError("Session expired. Run `roar login`.")
    return auth_state.access_token


def _should_refresh(auth_state) -> bool:
    if not auth_state.refresh_token:
        return False
    expires_at = auth_state.expires_at
    if not expires_at:
        return False
    expiry = _parse_iso8601(expires_at)
    if expiry is None:
        return False
    return expiry <= datetime.now(timezone.utc) + timedelta(minutes=5)


def _refresh_stored_access_token(auth_state) -> str:
    auth_api_url = resolve_auth_api_url()
    try:
        token_response = refresh_access_token(
            auth_api_url,
            auth_state.refresh_token or "",
            client_id=auth_state.client_id or "roar-cli",
        )
    except GlaasAuthError as exc:
        raise GlaasClientError(f"Session refresh failed: {exc}") from exc

    raw_data = _load_raw_auth_state_data()
    user_payload: dict[str, Any]
    if isinstance(raw_data.get("user"), dict):
        user_payload = dict(raw_data["user"])
    else:
        user_payload = {}
    access_context = token_response.raw_data.get("access_context")
    if isinstance(access_context, dict):
        user = access_context.get("user")
        if isinstance(user, dict):
            user_payload = {
                **user_payload,
                "sub": user.get("sub") or user_payload.get("sub"),
                "db_user_id": user.get("id") or user_payload.get("db_user_id"),
                "email": user.get("email") or user_payload.get("email"),
                "username": user.get("username") or user_payload.get("username"),
            }

    updated_raw = {
        **raw_data,
        "version": raw_data.get("version", 1),
        "provider": token_response.provider or raw_data.get("provider") or auth_state.provider,
        "issuer": token_response.issuer or raw_data.get("issuer") or auth_state.issuer,
        "client_id": token_response.client_id or raw_data.get("client_id") or auth_state.client_id,
        "access_token": token_response.access_token,
        "refresh_token": token_response.refresh_token or auth_state.refresh_token,
        "refresh_expires_at": token_response.refresh_expires_at
        or raw_data.get("refresh_expires_at"),
        "id_token": token_response.id_token or raw_data.get("id_token"),
        "expires_at": _compute_expires_at(token_response.expires_in) or raw_data.get("expires_at"),
        "user": user_payload,
    }
    save_auth_state(updated_raw)
    return token_response.access_token


def _load_raw_auth_state_data() -> dict[str, Any]:
    path = auth_store_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GlaasClientError("Stored auth state is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GlaasClientError("Stored auth state must be a JSON object")
    return payload


def _parse_iso8601(value: str) -> datetime | None:
    normalized = value.strip()
    if not normalized:
        return None
    normalized = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GlaasClientError(f"Stored auth state has invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compute_expires_at(expires_in: float | None) -> str | None:
    if expires_in is None:
        return None
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=max(expires_in, 0.0)))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
