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

from ...db.context import create_database_context
from ...services.labels import LabelService, parse_label_pairs, render_label_lines
from ..context import RoarContext
from ..decorators import require_init

_ENTITY_TYPE = click.Choice(["dag", "job", "artifact"], case_sensitive=False)


def _echo_current(metadata: dict, *, heading: str | None = None) -> None:
    if heading:
        click.echo(heading)
    lines = render_label_lines(metadata, indent="  " if heading else "")
    if not lines:
        click.echo("No labels.")
        return
    for line in lines:
        click.echo(line)


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
    with create_database_context(ctx.roar_dir) as db_ctx:
        service = LabelService(db_ctx, ctx.cwd)
        resolved = service.resolve_target(entity_type, target)
        patch = parse_label_pairs(pairs)
        result = service.set_metadata(resolved, patch)
        heading = (
            f"Updated labels (version {result.version}):"
            if result.changed
            else f"Labels unchanged (version {result.version}):"
        )
        _echo_current(result.metadata, heading=heading)


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
    with create_database_context(ctx.roar_dir) as db_ctx:
        service = LabelService(db_ctx, ctx.cwd)
        source = service.resolve_target(source_entity_type, source_target)
        destination = service.resolve_target(destination_entity_type, destination_target)
        result = service.copy_metadata(source, destination)
        heading = (
            f"Copied labels (version {result.version}):"
            if result.changed
            else f"Copy made no changes (version {result.version}):"
        )
        _echo_current(result.metadata, heading=heading)


@label.command("show")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.pass_obj
@require_init
def label_show(ctx: RoarContext, entity_type: str, target: str) -> None:
    """Show the current local label document for a target."""
    with create_database_context(ctx.roar_dir) as db_ctx:
        service = LabelService(db_ctx, ctx.cwd)
        resolved = service.resolve_target(entity_type, target)
        _echo_current(service.current_metadata(resolved))


@label.command("history")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.pass_obj
@require_init
def label_history(ctx: RoarContext, entity_type: str, target: str) -> None:
    """Show all local label versions for a target."""
    with create_database_context(ctx.roar_dir) as db_ctx:
        service = LabelService(db_ctx, ctx.cwd)
        resolved = service.resolve_target(entity_type, target)
        history = service.history(resolved)
        if not history:
            click.echo("No labels.")
            return
        for idx, row in enumerate(history):
            if idx:
                click.echo("")
            click.echo(f"Version {row['version']}:")
            for line in render_label_lines(row["metadata"], indent="  "):
                click.echo(line)
