from __future__ import annotations

import click

from ...auth_store import is_auth_state_expired, load_auth_state
from ...glaas_auth import resolve_auth_api_url
from ...glaas_client import GlaasClientError, fetch_access_context
from ...integrations.config import get_config_path_for_write, load_config, save_config
from ...scope_config import RepoScope, clear_repo_scope, load_repo_scope, save_repo_scope


@click.group("scope", invoke_without_command=True)
@click.pass_context
def scope(ctx: click.Context) -> None:
    """Show or change where this repo publishes lineage."""
    if ctx.invoked_subcommand is None:
        _render_scope_status()


@scope.command("status")
def scope_status() -> None:
    """Show the active repo publication scope."""
    _render_scope_status()


@scope.command("list")
@click.option(
    "--glaas-api-url",
    required=False,
    help="Override the GLaaS API base URL used to list project scopes.",
)
def scope_list(glaas_api_url: str | None) -> None:
    """List built-in scopes and accessible project scopes."""
    current = load_repo_scope()
    auth_state = load_auth_state()
    logged_in = auth_state is not None and not is_auth_state_expired(auth_state)

    rows = [
        _scope_row("anonymous", "none", "public anonymous", True, current),
        _scope_row("private", "login", "private personal", logged_in, current),
        _scope_row("public", "login", "public attributed", logged_in, current),
    ]

    if logged_in:
        resolved_api_url = resolve_auth_api_url(glaas_api_url)
        try:
            for project in _list_project_scope_options(resolved_api_url):
                rows.append(
                    {
                        "scope": project["scope_name"],
                        "auth": "login",
                        "visibility": project["visibility"],
                        "available": project["available"],
                        "active": (
                            "yes"
                            if current is not None
                            and current.mode == "project"
                            and current.project_id == project["project_id"]
                            else ""
                        ),
                    }
                )
        except GlaasClientError as exc:
            rows.append(
                {
                    "scope": "<projects>",
                    "auth": "login",
                    "visibility": f"unavailable: {exc}",
                    "available": "no",
                    "active": "",
                }
            )
    else:
        rows.append(
            {
                "scope": "<projects>",
                "auth": "login",
                "visibility": "project private",
                "available": "no",
                "active": "",
            }
        )

    _render_rows(rows)


@scope.command("use")
@click.argument("name")
@click.option(
    "--glaas-api-url",
    required=False,
    help="Override the GLaaS API base URL used to resolve a project scope.",
)
def scope_use(name: str, glaas_api_url: str | None) -> None:
    """Set this repo's default publication scope."""
    normalized = name.strip()
    if normalized in {"anonymous", "private", "public"}:
        path = save_repo_scope(normalized)  # type: ignore[arg-type]
        click.echo(f"Set roar scope to {normalized}.")
        click.echo(f"Saved to {path}")
        return

    if not normalized:
        raise click.ClickException("Project scope requires a project ID.")

    resolved_api_url = resolve_auth_api_url(glaas_api_url)
    try:
        owner_id, owner_type, project_id, display_name = _resolve_project_scope_from_access_context(
            glaas_api_url=resolved_api_url,
            requested_scope=normalized,
        )
    except GlaasClientError as exc:
        raise click.ClickException(str(exc)) from exc

    config = load_config()
    config["treqs"] = {
        "owner_id": owner_id,
        "owner_type": owner_type,
        "project_id": project_id,
    }
    config["scope"] = {"mode": "project"}
    config_path = get_config_path_for_write()
    save_config(config, config_path, preserve_existing=config)

    click.echo(f"Set roar scope to {display_name}.")
    click.echo(f"Resolved project {project_id} to {owner_type} {owner_id}")
    click.echo(f"Saved to {config_path}")


@scope.command("clear")
def scope_clear() -> None:
    """Clear this repo's durable scope and legacy project binding."""
    config_path = clear_repo_scope()
    config = load_config(config_path=config_path)
    config["treqs"] = {}
    save_config(config, config_path, preserve_existing=config)
    click.echo("Cleared roar scope.")
    click.echo(f"Saved to {config_path}")


def _render_scope_status() -> None:
    current = load_repo_scope()
    auth_state = load_auth_state()
    logged_in = auth_state is not None and not is_auth_state_expired(auth_state)
    identity = "logged in" if logged_in else "not logged in"

    click.echo(f"active: {_format_scope(current)}")
    click.echo(f"auth:   {identity}")
    if current is None:
        click.echo("hint: choose a scope with `roar scope use private`, `public`, or a project ID.")
    elif current.mode == "private" and not logged_in:
        click.echo("hint: private personal registration requires `roar login`.")
    elif current.mode == "anonymous":
        click.echo("hint: anonymous scope publishes publicly without account attribution.")


def _format_scope(scope: RepoScope | None) -> str:
    if scope is None:
        return "not set"
    if scope.mode == "project":
        suffix = f" ({scope.source})" if scope.source == "legacy_treqs" else ""
        return f"project {scope.project_id or '<unbound>'}{suffix}"
    return scope.mode


def _scope_row(
    name: str,
    auth: str,
    visibility: str,
    available: bool,
    current: RepoScope | None,
) -> dict[str, str]:
    return {
        "scope": name,
        "auth": auth,
        "visibility": visibility,
        "available": "yes" if available else "no",
        "active": "yes" if current is not None and current.mode == name else "",
    }


def _list_project_scope_options(glaas_api_url: str) -> list[dict[str, str]]:
    access_context = fetch_access_context(glaas_api_url)
    owners_by_id = _owners_by_id(access_context)
    projects_by_owner = _projects_by_owner(access_context)

    rows: list[dict[str, str]] = []
    for owner_id, owner_projects in projects_by_owner.items():
        owner = owners_by_id.get(owner_id)
        if owner is None:
            continue
        owner_slug = _owner_scope_name(owner, owner_id)

        for project in owner_projects:
            project_id = _string_value(project.get("id"))
            if project_id is None:
                continue
            project_slug = _project_scope_name(project, project_id)
            can_write = project.get("can_write") is not False
            rows.append(
                {
                    "scope_name": f"{owner_slug}/{project_slug}",
                    "project_id": project_id,
                    "visibility": str(project.get("visibility") or "project"),
                    "available": "yes" if can_write else "read-only",
                }
            )

    return rows


def _resolve_project_scope_from_access_context(
    *, glaas_api_url: str, requested_scope: str
) -> tuple[str, str, str, str]:
    access_context = fetch_access_context(glaas_api_url)
    owners_by_id = _owners_by_id(access_context)
    projects_by_owner = _projects_by_owner(access_context)

    owner_token: str | None
    project_token: str
    if "/" in requested_scope:
        owner_token, project_token = requested_scope.split("/", 1)
    else:
        owner_token, project_token = None, requested_scope
    owner_token = _normalize_token(owner_token)
    project_token = _normalize_token(project_token)
    if not project_token:
        raise click.ClickException("Project scope requires a project name or ID.")

    matches: list[tuple[str, str, str, str, bool]] = []
    for owner_id, owner_projects in projects_by_owner.items():
        owner = owners_by_id.get(owner_id)
        if owner is None:
            continue
        owner_type = _string_value(owner.get("type"))
        if owner_type is None:
            raise click.ClickException(
                "Invalid GLaaS auth access-context response: owner type missing"
            )
        if owner_token is not None and not _matches_owner_token(owner, owner_id, owner_token):
            continue

        owner_slug = _owner_scope_name(owner, owner_id)
        for project in owner_projects:
            project_id = _string_value(project.get("id"))
            if project_id is None:
                continue
            if not _matches_project_token(project, project_id, project_token):
                continue
            project_slug = _project_scope_name(project, project_id)
            matches.append(
                (
                    owner_id,
                    owner_type,
                    project_id,
                    f"{owner_slug}/{project_slug}",
                    project.get("can_write") is not False,
                )
            )

    if not matches:
        raise click.ClickException(
            f"Project scope not available in GLaaS auth access context: {requested_scope}"
        )
    if len(matches) > 1:
        raise click.ClickException(
            f"Project scope {requested_scope} is ambiguous. Use <owner>/<project>."
        )

    owner_id, owner_type, project_id, display_name, can_write = matches[0]
    if not can_write:
        raise click.ClickException(
            f"Project is visible but not writable in GLaaS auth access context: {display_name}"
        )

    return owner_id, owner_type, project_id, display_name


def _owners_by_id(access_context: dict[str, object]) -> dict[str, dict[str, object]]:
    owners = access_context.get("owners")
    if not isinstance(owners, list):
        raise click.ClickException("Invalid GLaaS auth access-context response: owners missing")

    owners_by_id: dict[str, dict[str, object]] = {}
    for owner in owners:
        if isinstance(owner, dict) and isinstance(owner.get("id"), str):
            owners_by_id[owner["id"]] = owner
    return owners_by_id


def _projects_by_owner(access_context: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    projects_by_owner = access_context.get("projects_by_owner")
    if not isinstance(projects_by_owner, dict):
        raise click.ClickException(
            "Invalid GLaaS auth access-context response: projects_by_owner missing"
        )

    parsed: dict[str, list[dict[str, object]]] = {}
    for owner_id, owner_projects in projects_by_owner.items():
        if not isinstance(owner_id, str) or not isinstance(owner_projects, list):
            raise click.ClickException(
                "Invalid GLaaS auth access-context response: owner projects invalid"
            )
        parsed_projects: list[dict[str, object]] = []
        for project in owner_projects:
            if not isinstance(project, dict):
                raise click.ClickException(
                    "Invalid GLaaS auth access-context response: owner projects invalid"
                )
            parsed_projects.append(project)
        parsed[owner_id] = parsed_projects
    return parsed


def _owner_scope_name(owner: dict[str, object], owner_id: str) -> str:
    return (
        _string_value(owner.get("username"))
        or _slugify(_string_value(owner.get("display_name")))
        or owner_id
    )


def _project_scope_name(project: dict[str, object], project_id: str) -> str:
    return _string_value(project.get("slug")) or _string_value(project.get("name")) or project_id


def _matches_owner_token(owner: dict[str, object], owner_id: str, token: str) -> bool:
    candidates = [
        owner_id,
        _string_value(owner.get("username")),
        _string_value(owner.get("display_name")),
        _slugify(_string_value(owner.get("display_name"))),
    ]
    return token in {_normalize_token(candidate) for candidate in candidates if candidate}


def _matches_project_token(project: dict[str, object], project_id: str, token: str) -> bool:
    candidates = [
        project_id,
        _string_value(project.get("slug")),
        _string_value(project.get("name")),
    ]
    return token in {_normalize_token(candidate) for candidate in candidates if candidate}


def _string_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_token(value: str | None) -> str:
    return (value or "").strip().lower()


def _slugify(value: str | None) -> str | None:
    normalized = _normalize_token(value)
    if not normalized:
        return None
    return "-".join(normalized.split())


def _render_rows(rows: list[dict[str, str]]) -> None:
    headers = (
        ("scope", "SCOPE"),
        ("auth", "AUTH"),
        ("visibility", "VISIBILITY"),
        ("available", "AVAILABLE"),
        ("active", "ACTIVE"),
    )
    widths = {key: max(len(label), *(len(row[key]) for row in rows)) for key, label in headers}
    click.echo("  ".join(label.ljust(widths[key]) for key, label in headers))
    for row in rows:
        click.echo("  ".join(row[key].ljust(widths[key]) for key, _label in headers))
