from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

    binding = _load_repo_binding(start_dir)
    if binding and not access_token:
        raise RuntimeError(
            "Repo is linked to GLaaS but no global auth state is available. Run `roar login`."
        )
    if not binding and not allow_public_without_binding:
        raise RuntimeError(
            "No GLaaS repo binding found for this publish. Link the repo to a TReqs owner/project first, or rerun with --public to publish publicly."
        )

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
    )


def resolve_publish_creator_identity(context: PublishAuthContext) -> str:
    provider = (context.auth_provider or "").strip().lower()
    if provider.startswith("treqs") and context.user_sub:
        return f"treqs:user:{context.user_sub}"
    if context.db_user_id:
        return f"glaas:user:{context.db_user_id}"
    return "anonymous"


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
