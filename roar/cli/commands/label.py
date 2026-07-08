"""
Label command group.

Usage:
    roar label set <dag|job|composite|artifact> <target> key=value [...]
    roar label unset <dag|job|composite|artifact> <target> key [...]
    roar label cp <dag|job|composite|artifact> <source> <dag|job|composite|artifact> <dest>
    roar label show <dag|job|composite|artifact> <target>
    roar label history <dag|job|composite|artifact> <target>
    roar label sync [dag|job|composite|artifact] [target]

Local mode (default) edits the versioned label documents in `.roar/roar.db`;
`roar label sync` pushes them to GLaaS. With `--remote`, set/unset/show/history
operate directly on GLaaS by remote identifiers (session hash, job uid,
artifact/composite hash) — no local project or session required.
"""

from __future__ import annotations

import click

from ...application.query.label import (
    copy_labels,
    remote_label_history,
    remote_set_labels,
    remote_show_labels,
    remote_unset_labels,
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
    RemoteLabelHistoryRequest,
    RemoteLabelSetRequest,
    RemoteLabelShowRequest,
    RemoteLabelUnsetRequest,
)
from ..context import RoarContext
from ..decorators import ensure_initialized, require_init

_ENTITY_TYPE = click.Choice(["dag", "job", "composite", "artifact"], case_sensitive=False)

_REMOTE_HELP = (
    "Edit labels directly on GLaaS by remote identifiers "
    "(session hash, job uid, artifact hash); no local project required."
)
_SESSION_HELP = "Session hash for --remote job targets (optional for artifact targets)."


def _local_entity_type(entity_type: str) -> str:
    """Composite artifacts are labeled as artifact targets."""
    normalized = entity_type.strip().lower()
    return "artifact" if normalized == "composite" else normalized


def _require_remote_for_session(remote: bool, session_hash: str | None) -> None:
    if session_hash and not remote:
        raise ValueError("--session is only valid together with --remote.")


@click.group("label", invoke_without_command=True)
@click.pass_context
def label(ctx: click.Context) -> None:
    """Manage labels locally and on GLaaS."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@label.command("set")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.argument("pairs", nargs=-1, required=True)
@click.option("--remote", is_flag=True, help=_REMOTE_HELP)
@click.option("--session", "session_hash", default=None, help=_SESSION_HELP)
@click.pass_obj
def label_set(
    ctx: RoarContext,
    entity_type: str,
    target: str,
    pairs: tuple[str, ...],
    remote: bool,
    session_hash: str | None,
) -> None:
    """Patch the current label document for a target."""
    try:
        _require_remote_for_session(remote, session_hash)
        if remote:
            rendered = remote_set_labels(
                RemoteLabelSetRequest(
                    cwd=ctx.cwd,
                    entity_type=entity_type,
                    target=target,
                    pairs=pairs,
                    session_hash=session_hash,
                )
            )
        else:
            ensure_initialized(ctx)
            rendered = set_labels(
                LabelSetRequest(
                    roar_dir=ctx.roar_dir,
                    cwd=ctx.cwd,
                    entity_type=_local_entity_type(entity_type),
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
@click.option("--remote", is_flag=True, help=_REMOTE_HELP)
@click.option("--session", "session_hash", default=None, help=_SESSION_HELP)
@click.pass_obj
def label_unset(
    ctx: RoarContext,
    entity_type: str,
    target: str,
    keys: tuple[str, ...],
    remote: bool,
    session_hash: str | None,
) -> None:
    """Remove label keys from the current label document for a target."""
    try:
        _require_remote_for_session(remote, session_hash)
        if remote:
            rendered = remote_unset_labels(
                RemoteLabelUnsetRequest(
                    cwd=ctx.cwd,
                    entity_type=entity_type,
                    target=target,
                    keys=keys,
                    session_hash=session_hash,
                )
            )
        else:
            ensure_initialized(ctx)
            rendered = unset_labels(
                LabelUnsetRequest(
                    roar_dir=ctx.roar_dir,
                    cwd=ctx.cwd,
                    entity_type=_local_entity_type(entity_type),
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
                source_entity_type=_local_entity_type(source_entity_type),
                source_target=source_target,
                destination_entity_type=_local_entity_type(destination_entity_type),
                destination_target=destination_target,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@label.command("show")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.option("--remote", is_flag=True, help=_REMOTE_HELP)
@click.option("--session", "session_hash", default=None, help=_SESSION_HELP)
@click.pass_obj
def label_show(
    ctx: RoarContext,
    entity_type: str,
    target: str,
    remote: bool,
    session_hash: str | None,
) -> None:
    """Show the current label document for a target."""
    try:
        _require_remote_for_session(remote, session_hash)
        if remote:
            rendered = remote_show_labels(
                RemoteLabelShowRequest(
                    cwd=ctx.cwd,
                    entity_type=entity_type,
                    target=target,
                    session_hash=session_hash,
                )
            )
        else:
            ensure_initialized(ctx)
            rendered = show_labels(
                LabelShowRequest(
                    roar_dir=ctx.roar_dir,
                    cwd=ctx.cwd,
                    entity_type=_local_entity_type(entity_type),
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
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the confirmation prompt when the sync would delete remote label keys.",
)
@click.pass_obj
@require_init
def label_sync(
    ctx: RoarContext,
    entity_type: str | None,
    target: str | None,
    dry_run: bool,
    output_json: bool,
    yes: bool,
) -> None:
    """Sync current local user-managed labels to GLaaS.

    Pushes current user labels and propagates local `label unset` removals as
    remote key deletions (keys unset since the last successful sync). If any
    keys would be deleted remotely, you will be prompted to confirm unless
    --yes or --dry-run is given.
    """
    try:
        rendered = sync_labels(
            LabelSyncRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                entity_type=_local_entity_type(entity_type) if entity_type else entity_type,
                target=target,
                dry_run=dry_run,
                output_json=output_json,
                skip_confirmation=yes,
            )
        )
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@label.command("history")
@click.argument("entity_type", type=_ENTITY_TYPE)
@click.argument("target")
@click.option("--remote", is_flag=True, help=_REMOTE_HELP)
@click.option("--session", "session_hash", default=None, help=_SESSION_HELP)
@click.pass_obj
def label_history(
    ctx: RoarContext,
    entity_type: str,
    target: str,
    remote: bool,
    session_hash: str | None,
) -> None:
    """Show all label versions for a target."""
    try:
        _require_remote_for_session(remote, session_hash)
        if remote:
            rendered = remote_label_history(
                RemoteLabelHistoryRequest(
                    cwd=ctx.cwd,
                    entity_type=entity_type,
                    target=target,
                    session_hash=session_hash,
                )
            )
        else:
            ensure_initialized(ctx)
            rendered = render_label_history(
                LabelHistoryRequest(
                    roar_dir=ctx.roar_dir,
                    cwd=ctx.cwd,
                    entity_type=_local_entity_type(entity_type),
                    target=target,
                )
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)
