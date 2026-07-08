"""
Local label command group.

Usage:
    roar label set <dag|job|artifact> <target> key=value [...]
    roar label unset <dag|job|artifact> <target> key [...]
    roar label cp <dag|job|artifact> <source> <dag|job|artifact> <dest>
    roar label show <dag|job|artifact> <target>
    roar label history <dag|job|artifact> <target>
    roar label sync [dag|job|artifact] [target]
"""

from __future__ import annotations

import click

from ...application.query.label import (
    copy_labels,
    set_labels,
    show_labels,
    sync_labels,
    unset_labels,
)
from ...application.query.label import (
    label_history as render_label_history,
)
from ...application.query.requests import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelSetRequest,
    LabelShowRequest,
    LabelSyncRequest,
    LabelUnsetRequest,
)
from ..context import RoarContext
from ..decorators import require_init

_ENTITY_TYPE = click.Choice(["dag", "job", "artifact"], case_sensitive=False)


@click.group("label", invoke_without_command=True)
@click.pass_context
def label(ctx: click.Context) -> None:
    """Manage local labels and sync user-managed label updates to GLaaS."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@label.command("set")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.argument("pairs", nargs=-1, required=True)
@click.pass_obj
@require_init
def label_set(ctx: RoarContext, entity_type: str, target: str, pairs: tuple[str, ...]) -> None:
    """Patch the current label document for a target."""
    try:
        rendered = set_labels(
            LabelSetRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                entity_type=entity_type,
                target=target,
                pairs=pairs,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@label.command("unset")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.argument("keys", nargs=-1, required=True)
@click.pass_obj
@require_init
def label_unset(ctx: RoarContext, entity_type: str, target: str, keys: tuple[str, ...]) -> None:
    """Remove label keys from the current local label document for a target."""
    try:
        rendered = unset_labels(
            LabelUnsetRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                entity_type=entity_type,
                target=target,
                keys=keys,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@label.command("cp")
@click.argument("source_entity_type", type=_ENTITY_TYPE)
@click.argument("source_target")
@click.argument("destination_entity_type", type=_ENTITY_TYPE)
@click.argument("destination_target")
@click.pass_obj
@require_init
def label_cp(
    ctx: RoarContext,
    source_entity_type: str,
    source_target: str,
    destination_entity_type: str,
    destination_target: str,
) -> None:
    """Copy the current source label document into the destination as a patch."""
    try:
        rendered = copy_labels(
            LabelCopyRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                source_entity_type=source_entity_type,
                source_target=source_target,
                destination_entity_type=destination_entity_type,
                destination_target=destination_target,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@label.command("show")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.pass_obj
@require_init
def label_show(ctx: RoarContext, entity_type: str, target: str) -> None:
    """Show the current local label document for a target."""
    try:
        rendered = show_labels(
            LabelShowRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                entity_type=entity_type,
                target=target,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@label.command("sync")
@click.argument("entity_type", type=_ENTITY_TYPE, required=False)
@click.argument("target", required=False)
@click.option("--dry-run", is_flag=True, help="Preview remote reconcile without writing.")
@click.option("--json", "output_json", is_flag=True, help="Render the GLaaS reconcile response.")
@click.pass_obj
@require_init
def label_sync(
    ctx: RoarContext,
    entity_type: str | None,
    target: str | None,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Sync current local user-managed labels to GLaaS.

    Pushes current user labels and propagates local `label unset` removals as
    remote key deletions (keys unset since the last successful sync).
    """
    try:
        rendered = sync_labels(
            LabelSyncRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                entity_type=entity_type,
                target=target,
                dry_run=dry_run,
                output_json=output_json,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@label.command("history")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.pass_obj
@require_init
def label_history(ctx: RoarContext, entity_type: str, target: str) -> None:
    """Show all local label versions for a target."""
    try:
        rendered = render_label_history(
            LabelHistoryRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                entity_type=entity_type,
                target=target,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)
