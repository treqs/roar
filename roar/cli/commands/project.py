from __future__ import annotations

import click

from ...integrations.config import get_config_path_for_write, load_config, save_config
from ...treqs_client import TreqsClientError, fetch_access_context


@click.group("project", invoke_without_command=True)
@click.pass_context
def project(ctx: click.Context) -> None:
    """Manage repo-local treqs project binding."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@project.command("link")
@click.option("--owner-id", required=True, help="Treqs owner identifier for this repo binding.")
@click.option(
    "--owner-type",
    type=click.Choice(["user", "organization"], case_sensitive=False),
    required=True,
    help="Owner type for the selected owner.",
)
@click.option("--project-id", required=False, help="Optional treqs project identifier.")
@click.option(
    "--treqs-api-url",
    required=False,
    help="Validate owner/project against the treqs access-context endpoint before saving.",
)
def project_link(
    owner_id: str,
    owner_type: str,
    project_id: str | None,
    treqs_api_url: str | None,
) -> None:
    """Write repo-local treqs owner/project binding to .roar/config.toml."""
    if treqs_api_url:
        _validate_binding_against_treqs(
            treqs_api_url=treqs_api_url,
            owner_id=owner_id,
            owner_type=owner_type,
            project_id=project_id,
        )
        click.echo("Validated binding against treqs access context")

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


def _validate_binding_against_treqs(
    *, treqs_api_url: str, owner_id: str, owner_type: str, project_id: str | None
) -> None:
    try:
        access_context = fetch_access_context(treqs_api_url)
    except TreqsClientError as exc:
        raise click.ClickException(str(exc)) from exc

    owners = access_context.get("owners")
    if not isinstance(owners, list):
        raise click.ClickException("Invalid treqs access-context response: owners missing")

    matched_owner = None
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        if owner.get("id") == owner_id and owner.get("type") == owner_type:
            matched_owner = owner
            break

    if matched_owner is None:
        raise click.ClickException(f"Owner not available in treqs access context: {owner_type} {owner_id}")

    if owner_type == "organization" and matched_owner.get("role") not in {"owner", "admin"}:
        raise click.ClickException(
            f"Organization binding requires owner or admin role: {owner_id}"
        )

    if project_id is None:
        return

    projects_by_owner = access_context.get("projects_by_owner")
    if not isinstance(projects_by_owner, dict):
        raise click.ClickException("Invalid treqs access-context response: projects_by_owner missing")

    owner_projects = projects_by_owner.get(owner_id, [])
    if not isinstance(owner_projects, list):
        raise click.ClickException("Invalid treqs access-context response: owner projects invalid")

    for project in owner_projects:
        if not isinstance(project, dict) or project.get("id") != project_id:
            continue
        if project.get("can_write") is False:
            raise click.ClickException(
                f"Project is visible but not writable in treqs access context: {project_id}"
            )
        return

    raise click.ClickException(f"Project not available in treqs access context: {project_id}")
