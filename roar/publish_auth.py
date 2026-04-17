from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

from .auth_store import load_auth_state


@dataclass(frozen=True)
class PublishAuthContext:
    access_token: str | None
    scope_request: dict[str, str] | None
    auth_provider: str | None = None
    user_sub: str | None = None
    db_user_id: str | None = None
    creator_identity: str | None = None


def load_publish_auth_context(
    start_dir: str | Path | None = None,
    *,
    allow_public_without_binding: bool = False,
) -> PublishAuthContext:
    access_token = None
    auth_provider = None
    user_sub = None
    db_user_id = None
    auth_state = load_auth_state()
    if auth_state is not None:
        access_token = auth_state.access_token
        auth_provider = auth_state.provider
        user_sub = auth_state.user.sub or None
        db_user_id = auth_state.user.db_user_id

    binding = None if allow_public_without_binding else _load_repo_binding(start_dir)
    if binding and not access_token:
        raise RuntimeError(
            "Repo is linked to GLaaS but no global auth state is available. Run `roar login`."
        )
    if not binding and not allow_public_without_binding:
        raise RuntimeError(
            "No GLaaS repo binding found for this publish. Link the repo to a TReqs owner/project first, or rerun with --public to publish publicly."
        )

    creator_identity = None
    if not access_token and allow_public_without_binding:
        creator_identity, resolved_db_user_id = _load_authenticated_creator_identity()
        if resolved_db_user_id and not db_user_id:
            db_user_id = resolved_db_user_id

    scope_request = None
    if binding:
        scope_request = {
            "owner_id": binding["owner_id"],
            "owner_type": binding["owner_type"],
            "visibility": "private",
        }
        project_id = binding.get("project_id")
        if project_id:
            scope_request["project_id"] = project_id

    return PublishAuthContext(
        access_token=access_token,
        scope_request=scope_request,
        auth_provider=auth_provider,
        user_sub=user_sub,
        db_user_id=db_user_id,
        creator_identity=creator_identity,
    )


def resolve_publish_creator_identity(context: PublishAuthContext) -> str:
    explicit_identity = _optional_string(context.creator_identity)
    if explicit_identity is not None:
        return explicit_identity

    provider = (context.auth_provider or "").strip().lower()
    if provider.startswith("treqs") and context.user_sub:
        return f"treqs:user:{context.user_sub}"
    if context.db_user_id:
        return f"glaas:user:{context.db_user_id}"
    return "anonymous"


def _load_authenticated_creator_identity() -> tuple[str | None, str | None]:
    from .integrations.glaas import get_glaas_url, make_auth_header

    base_url = _optional_string(get_glaas_url())
    if base_url is None:
        return None, None

    path = "/api/v1/auth/me"
    auth_header = make_auth_header("GET", path, None)
    if not auth_header:
        return None, None

    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}")
    request.add_header("Authorization", auth_header)
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None, None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, None

    creator_identity = _optional_string(
        data.get("creatorIdentity") if isinstance(data, dict) else None
    ) or _optional_string(data.get("creator_identity") if isinstance(data, dict) else None)

    user = data.get("user")
    db_user_id = _optional_string(user.get("id")) if isinstance(user, dict) else None
    if creator_identity is None and db_user_id is not None:
        creator_identity = f"glaas:user:{db_user_id}"

    return creator_identity, db_user_id


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _load_repo_binding(start_dir: str | Path | None = None) -> dict[str, str] | None:
    config_path = _find_repo_config(start_dir)
    if config_path is None or not config_path.exists():
        return None

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    treqs = data.get("treqs")
    if not isinstance(treqs, dict):
        return None

    owner_id = treqs.get("owner_id")
    owner_type = treqs.get("owner_type")
    project_id = treqs.get("project_id")
    if not isinstance(owner_id, str) or not owner_id.strip():
        return None
    if not isinstance(owner_type, str) or owner_type not in {"user", "organization"}:
        return None

    binding: dict[str, str] = {
        "owner_id": owner_id,
        "owner_type": owner_type,
    }
    if isinstance(project_id, str) and project_id.strip():
        binding["project_id"] = project_id
    return binding


def _find_repo_config(start_dir: str | Path | None = None) -> Path | None:
    start = Path(start_dir) if start_dir else Path.cwd()
    for parent in [start, *list(start.parents)]:
        config_path = parent / ".roar" / "config.toml"
        if config_path.exists():
            return config_path
    return None
