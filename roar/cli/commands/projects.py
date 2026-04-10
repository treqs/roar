from __future__ import annotations

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

import click

from ...glaas_auth import resolve_auth_api_url
from ...glaas_client import GlaasClientError, fetch_access_context, fetch_user_projects
from ...integrations.config import get_config_path_for_write, load_config, save_config
from ...integrations.config.loader import find_config_file


@click.group("projects", invoke_without_command=True)
@click.pass_context
def projects(ctx: click.Context) -> None:
    """Inspect and manage GLaaS projects."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@projects.command("link")
@click.argument("project_id")
@click.option(
    "--glaas-api-url",
    required=False,
    help="Override the GLaaS API base URL used to resolve the project binding.",
)
def projects_link(project_id: str, glaas_api_url: str | None) -> None:
    """Link this repo to a GLaaS project by project ID."""
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
    config_path = get_config_path_for_write()
    save_config(config, config_path)

    click.echo(f"Resolved project {project_id} to {owner_type} {owner_id}")
    click.echo(f"Linked repo to {owner_type} {owner_id} / project {project_id}")
    click.echo(f"Saved to {config_path}")


@projects.command("list")
@click.option(
    "--glaas-api-url",
    required=False,
    help="Override the GLaaS API base URL used to list projects.",
)
def projects_list(glaas_api_url: str | None) -> None:
    """List accessible GLaaS projects."""
    resolved_api_url = resolve_auth_api_url(glaas_api_url)

    try:
        projects_data = fetch_user_projects(resolved_api_url)
    except GlaasClientError as exc:
        raise click.ClickException(str(exc)) from exc

    if not projects_data:
        click.echo("No projects found.")
        return

    rows = [
        {
            "id": str(project.get("id") or ""),
            "name": str(project.get("name") or ""),
            "description": str(project.get("description") or ""),
            "visibility": str(project.get("visibility") or ""),
        }
        for project in projects_data
        if isinstance(project, dict)
    ]

    if not rows:
        raise click.ClickException(
            "Invalid GLaaS projects response: expected a list of project objects"
        )

    headers = (
        ("id", "ID"),
        ("name", "NAME"),
        ("description", "DESCRIPTION"),
        ("visibility", "VISIBILITY"),
    )
    widths = {key: max(len(label), *(len(row[key]) for row in rows)) for key, label in headers}

    click.echo("  ".join(label.ljust(widths[key]) for key, label in headers))
    for row in rows:
        click.echo("  ".join(row[key].ljust(widths[key]) for key, _label in headers))


@projects.command("unlink")
def projects_unlink() -> None:
    """Remove this repo's GLaaS project binding."""
    config_path = get_config_path_for_write()
    existing_binding = _load_existing_repo_binding()

    if not isinstance(existing_binding, dict) or not existing_binding:
        click.echo("No repo project binding found.")
        return

    config = load_config()
    # Keep key present to avoid unknown-section preservation during save.
    config["treqs"] = {}
    save_config(config, config_path)

    click.echo("Unlinked repo project binding.")
    click.echo(f"Saved to {config_path}")


def _resolve_project_binding_from_access_context(
    *, glaas_api_url: str, project_id: str
) -> tuple[str, str]:
    access_context = fetch_access_context(glaas_api_url)

    owners = access_context.get("owners")
    if not isinstance(owners, list):
        raise click.ClickException("Invalid GLaaS auth access-context response: owners missing")

    owner_by_id: dict[str, dict[str, object]] = {}
    for owner in owners:
        if isinstance(owner, dict) and isinstance(owner.get("id"), str):
            owner_by_id[owner["id"]] = owner

    projects_by_owner = access_context.get("projects_by_owner")
    if not isinstance(projects_by_owner, dict):
        raise click.ClickException(
            "Invalid GLaaS auth access-context response: projects_by_owner missing"
        )

    matches: list[tuple[str, str]] = []
    for owner_id, owner_projects in projects_by_owner.items():
        if not isinstance(owner_id, str) or not isinstance(owner_projects, list):
            raise click.ClickException(
                "Invalid GLaaS auth access-context response: owner projects invalid"
            )

        owner = owner_by_id.get(owner_id)
        if owner is None:
            continue

        owner_type = owner.get("type")
        if not isinstance(owner_type, str):
            raise click.ClickException(
                "Invalid GLaaS auth access-context response: owner type missing"
            )

        for project in owner_projects:
            if not isinstance(project, dict) or project.get("id") != project_id:
                continue
            if project.get("can_write") is False:
                raise click.ClickException(
                    f"Project is visible but not writable in GLaaS auth access context: {project_id}"
                )
            matches.append((owner_id, owner_type))

    if not matches:
        raise click.ClickException(
            f"Project not available in GLaaS auth access context: {project_id}"
        )
    if len(matches) > 1:
        raise click.ClickException(
            f"Project ID {project_id} is ambiguous across multiple owners in GLaaS auth access context"
        )

    return matches[0]


def _load_existing_repo_binding() -> dict[str, object] | None:
    config_path = find_config_file()
    if config_path is None or config_path.name != "config.toml":
        return None

    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    binding = data.get("treqs")
    if not isinstance(binding, dict):
        return None
    return binding
