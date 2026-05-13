from __future__ import annotations

import click

from ...auth_store import is_auth_state_expired, load_auth_state
from ...glaas_auth import resolve_auth_api_url
from ...glaas_client import GlaasClientError, fetch_user_projects
from ...integrations.config import get_config_path_for_write, load_config, save_config
from ...scope_config import RepoScope, clear_repo_scope, load_repo_scope, save_repo_scope
from .projects import _resolve_project_binding_from_access_context


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
            for project in fetch_user_projects(resolved_api_url):
                project_id = str(project.get("id") or "")
                if not project_id:
                    continue
                rows.append(
                    {
                        "scope": project_id,
                        "auth": "login",
                        "visibility": str(project.get("visibility") or "project"),
                        "available": "yes",
                        "active": (
                            "yes"
                            if current is not None
                            and current.mode == "project"
                            and current.project_id == project_id
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

    project_id = normalized.split("/", 1)[1] if "/" in normalized else normalized
    if not project_id:
        raise click.ClickException("Project scope requires a project ID.")

    resolved_api_url = resolve_auth_api_url(glaas_api_url)
    try:
        owner_id, owner_type = _resolve_project_binding_from_access_context(
            glaas_api_url=resolved_api_url,
            project_id=project_id,
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

    click.echo(f"Set roar scope to project {project_id}.")
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
