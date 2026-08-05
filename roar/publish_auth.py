from __future__ import annotations

import contextvars
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
from .scope_config import load_repo_scope


class PublishAuthError(RuntimeError):
    """Raised when publish auth or repo binding requirements are not satisfied."""


@dataclass(frozen=True)
class PublishAuthContext:
    access_token: str | None
    scope_request: dict[str, str] | None
    auth_provider: str | None = None
    user_sub: str | None = None
    db_user_id: str | None = None
    creator_identity: str | None = None
    ssh_auth_available: bool = False


# Request-scoped carrier for the explicit --public/--private choice. The publish
# path builds several GlaasClients lazily and independently (session/artifact/job
# registrars each construct their own), so there is no single object to thread the
# flag through. The CLI records the choice here for the duration of one publish so
# every lazily-built client resolves the SAME, flag-reconciled scope_request. It
# defaults to None (no explicit choice) so non-publish callers are unaffected.
_requested_public: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "roar_requested_public", default=None
)


def set_requested_publish_visibility(public: bool | None) -> contextvars.Token:
    """Record the explicit ``--public``/``--private`` choice for this publish.

    ``public=None`` means no explicit flag (fall back to the scope default).
    Returns the token to pass to :func:`reset_requested_publish_visibility`.
    """
    return _requested_public.set(public)


def reset_requested_publish_visibility(token: contextvars.Token) -> None:
    _requested_public.reset(token)


def _base_scope_visibility(repo_scope: Any) -> str | None:
    """Visibility implied by the active repo scope alone, ignoring any flag.

    ``public`` -> ``"public"`` (attributed); ``anonymous`` -> ``None`` so the
    caller omits ``scope_request`` and the server uses the legacy anonymous
    public scope; a ``project`` scope follows the bound project's own
    visibility (``"public"`` when the project is public, otherwise
    ``"private"``); everything else (``private``/unset) -> ``"private"``.
    """
    if repo_scope is not None:
        if repo_scope.mode == "public":
            return "public"
        if repo_scope.mode == "anonymous":
            return None
        if repo_scope.mode == "project" and getattr(repo_scope, "visibility", None) == "public":
            return "public"
    return "private"


def _scope_visibility(repo_scope: Any, requested_public: bool | None = None) -> str | None:
    """Resolve scope_request visibility, reconciling scope with an explicit flag.

    Principle (a single, non-arbitrary rule): the ``--public``/``--private`` flag
    governs the DAG's visibility; it may always **tighten** to private, but it may
    not **escalate** a private *project* to a public DAG.

    - no explicit flag -> the scope default (unchanged behaviour);
    - ``--private`` -> ``"private"`` always (tightening is always safe);
    - ``--public`` -> ``"public"``, EXCEPT when bound to a private project, where
      it is rejected (publishing a public DAG from a private project would leak).

    The anonymous scope (``None``) is never overridden — private publishing needs
    an account, which the caller enforces separately.
    """
    base = _base_scope_visibility(repo_scope)
    if requested_public is None:
        requested_public = _requested_public.get()
    if base is None or requested_public is None:
        return base
    if requested_public is False:
        return "private"
    # requested_public is True (--public):
    if (
        base == "private"
        and repo_scope is not None
        and getattr(repo_scope, "mode", None) == "project"
    ):
        raise PublishAuthError(
            "This repo is bound to a private project, so --public would publish a "
            "public DAG from a private project. Bind a public project "
            "(`roar scope use <owner>/<public-project>`) or drop --public."
        )
    return "public"


def load_publish_auth_context(
    start_dir: str | Path | None = None,
    *,
    allow_public_without_binding: bool = False,
    force_anonymous: bool = False,
    requested_public: bool | None = None,
) -> PublishAuthContext:
    if force_anonymous:
        return PublishAuthContext(
            access_token=None,
            scope_request=None,
            auth_provider=None,
            user_sub=None,
            db_user_id=None,
            creator_identity=None,
            ssh_auth_available=False,
        )

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

    ssh_auth_available = _has_ssh_auth_credentials()

    # Proactively renew an expiring/expired bearer so register doesn't ride on a
    # token `roar whoami` already calls "expired" (which then reads as a bug when
    # it works anyway). If it can't be refreshed, drop it for an SSH fallback when
    # available, else fail clearly toward `roar login` rather than sending a dead
    # token.
    if auth_state is not None and access_token:
        from .glaas_client import GlaasClientError, ensure_fresh_access_token

        try:
            access_token, _refreshed = ensure_fresh_access_token(auth_state)
        except GlaasClientError as exc:
            if ssh_auth_available:
                access_token = None
            else:
                raise PublishAuthError(str(exc)) from exc
    binding = None if allow_public_without_binding else _load_repo_binding(start_dir)
    repo_scope = load_repo_scope(start_dir)
    # `allow_public_without_binding` permits a *scopeless* public publish, but it
    # must not discard a **public project scope** — that binding carries the org
    # attribution (supplier/author) the AI-BOM needs, and a public project's
    # whole point is public + attributed. Regression fix: eef6fca (0.4.1) made a
    # public project scope resolve to public=True, which the CLI feeds into
    # `allow_public_without_binding=request.public`; dropping repo_scope here then
    # published those DAGs anonymous. A *non-public* project scope with an
    # explicit `--public` still drops (the user is overriding to public), as do
    # public/anonymous/unset scopes.
    if allow_public_without_binding and not (
        repo_scope and repo_scope.mode == "project" and repo_scope.visibility == "public"
    ):
        repo_scope = None
    if binding and not access_token and not ssh_auth_available:
        raise PublishAuthError(
            "Repo is linked to GLaaS but no global auth state is available. Run `roar login`."
        )
    if not binding and repo_scope is not None and repo_scope.mode == "project":
        binding = {
            "owner_id": repo_scope.owner_id or "",
            "owner_type": repo_scope.owner_type or "",
        }
        if repo_scope.project_id:
            binding["project_id"] = repo_scope.project_id
        if not access_token and not ssh_auth_available:
            raise PublishAuthError(
                "Repo is linked to GLaaS but no global auth state is available. Run `roar login`."
            )

    if not binding and not allow_public_without_binding and not access_token:
        raise PublishAuthError(
            "Private registration requires GLaaS login when no project scope is linked. "
            "Run `roar login`, use `roar scope use <project-id>`, or rerun with --public."
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
            "visibility": _scope_visibility(repo_scope, requested_public) or "private",
        }
        project_id = binding.get("project_id")
        if project_id:
            scope_request["project_id"] = project_id
    elif not allow_public_without_binding:
        # Server-authoritative owner resolution; visibility follows the active
        # repo scope. The anonymous scope yields None here so we omit
        # scope_request entirely (server uses the legacy anonymous public scope).
        visibility = _scope_visibility(repo_scope, requested_public)
        if visibility is not None:
            scope_request = {
                "owner_resolution": "current_user",
                "owner_type": "user",
                "visibility": visibility,
            }

    return PublishAuthContext(
        access_token=access_token,
        scope_request=scope_request,
        auth_provider=auth_provider,
        user_sub=user_sub,
        db_user_id=db_user_id,
        creator_identity=creator_identity,
        ssh_auth_available=ssh_auth_available,
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


def _has_ssh_auth_credentials() -> bool:
    from .integrations.glaas.auth import find_ssh_private_key, find_ssh_pubkey

    return find_ssh_private_key() is not None and find_ssh_pubkey() is not None


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
