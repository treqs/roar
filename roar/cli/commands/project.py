from __future__ import annotations

import click

from ...glaas_auth import resolve_auth_api_url
from ...glaas_client import GlaasClientError, fetch_access_context
from ...integrations.config import get_config_path_for_write, load_config, save_config


@click.group("project", invoke_without_command=True)
@click.pass_context
def project(ctx: click.Context) -> None:
    """Manage repo-local GLaaS project binding."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@project.command("link")
@click.option("--owner-id", required=True, help="GLaaS owner identifier for this repo binding.")
@click.option(
    "--owner-type",
    type=click.Choice(["user", "organization"], case_sensitive=False),
    required=True,
    help="Owner type for the selected owner.",
)
@click.option("--project-id", required=False, help="Optional GLaaS project identifier.")
@click.option(
    "--glaas-api-url",
    required=False,
    help="Validate owner/project against the GLaaS auth access-context endpoint before saving.",
)
def project_link(
    owner_id: str,
    owner_type: str,
    project_id: str | None,
    glaas_api_url: str | None,
) -> None:
    """Write repo-local GLaaS owner/project binding to .roar/config.toml."""
    if glaas_api_url:
        _validate_binding_against_access_context(
            glaas_api_url=resolve_auth_api_url(glaas_api_url),
            owner_id=owner_id,
            owner_type=owner_type,
            project_id=project_id,
        )
        click.echo("Validated binding against GLaaS auth access context")

    config = load_config()
    config["treqs"] = {
        "owner_id": owner_id,
        "owner_type": owner_type,
        "project_id": project_id,
    }
    config_path = get_config_path_for_write()
    save_config(config, config_path)

    if project_id:
        click.echo(f"Linked repo to {owner_type} {owner_id} / project {project_id}")
    else:
        click.echo(f"Linked repo to {owner_type} {owner_id}")
    click.echo(f"Saved to {config_path}")


def _validate_binding_against_access_context(
    *, glaas_api_url: str, owner_id: str, owner_type: str, project_id: str | None
) -> None:
    try:
        access_context = fetch_access_context(glaas_api_url)
    except GlaasClientError as exc:
        raise click.ClickException(str(exc)) from exc

    owners = access_context.get("owners")
    if not isinstance(owners, list):
        raise click.ClickException("Invalid GLaaS auth access-context response: owners missing")

    matched_owner = None
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        if owner.get("id") == owner_id and owner.get("type") == owner_type:
            matched_owner = owner
            break

    if matched_owner is None:
        raise click.ClickException(
            f"Owner not available in GLaaS auth access context: {owner_type} {owner_id}"
        )

    if owner_type == "organization" and matched_owner.get("role") not in {"owner", "admin"}:
        raise click.ClickException(f"Organization binding requires owner or admin role: {owner_id}")

    if project_id is None:
        return

    projects_by_owner = access_context.get("projects_by_owner")
    if not isinstance(projects_by_owner, dict):
        raise click.ClickException(
            "Invalid GLaaS auth access-context response: projects_by_owner missing"
        )

    owner_projects = projects_by_owner.get(owner_id, [])
    if not isinstance(owner_projects, list):
        raise click.ClickException(
            "Invalid GLaaS auth access-context response: owner projects invalid"
        )

    for project in owner_projects:
        if not isinstance(project, dict) or project.get("id") != project_id:
            continue
        if project.get("can_write") is False:
            raise click.ClickException(
                f"Project is visible but not writable in GLaaS auth access context: {project_id}"
            )
        return

    raise click.ClickException(f"Project not available in GLaaS auth access context: {project_id}")
