"""
Local label command group.

Usage:
    roar label set <dag|job|artifact> <target> key=value [...]
    roar label cp <dag|job|artifact> <source> <dag|job|artifact> <dest>
    roar label show <dag|job|artifact> <target>
    roar label history <dag|job|artifact> <target>
"""

from __future__ import annotations

import click

from ...application.query.label import (
    copy_labels,
    set_labels,
    show_labels,
)
from ...application.query.label import (
    label_history as render_label_history,
)
from ...application.query.requests import (
    LabelCopyRequest,
    LabelHistoryRequest,
    LabelSetRequest,
    LabelShowRequest,
)
from ..context import RoarContext
from ..decorators import require_init

_ENTITY_TYPE = click.Choice(["dag", "job", "artifact"], case_sensitive=False)


@click.group("label", invoke_without_command=True)
@click.pass_context
def label(ctx: click.Context) -> None:
    """Manage local labels for DAGs, jobs, and artifacts."""
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
