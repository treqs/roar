"""
Compliance tag command group.

Usage:
    roar tag add  <kind>=<value>   <target>
    roar tag rm   <kind>[=<value>] <target>
    roar tag show   <target>
    roar tag history  <target>
    roar tag why    <kind>[=<value>] <target>
    roar tag bind    <artifact>...
    roar tag unbind  <artifact>...

Targets:
    @N          Job step N in the active session
    <hash>      Artifact by hash prefix or path

Canonical tag kinds:
    license  contains_pii  jurisdiction  classification  special_category

Scope: tags propagate automatically within a session. Crossing a session
boundary requires an explicit bind — `roar tag bind <artifact>` promotes
that artifact's current tags to cross-session scope. `roar tag add` on a
named artifact is "born bound" (writes an implicit bind); `roar run
--add-tag` stays session-scoped until the artifact is explicitly bound.
"""

from __future__ import annotations

import click

from ...application.query.requests import (
    TagAddRequest,
    TagBindRequest,
    TagHistoryRequest,
    TagRmRequest,
    TagShowRequest,
    TagUnbindRequest,
    TagWhyRequest,
)
from ...application.query.tag import (
    tag_add,
    tag_bind,
    tag_history,
    tag_rm,
    tag_show,
    tag_unbind,
    tag_why,
)
from ...core.label_constants import CANONICAL_TAG_KINDS
from ..context import RoarContext
from ..decorators import require_init


@click.group("tag", invoke_without_command=True)
@click.pass_context
def tag(ctx: click.Context) -> None:
    """Manage hereditary compliance tags on artifacts and jobs.

    Tags are stored under the tag.* label namespace and propagate to
    downstream artifacts through the lineage graph.

    \b
    Canonical kinds:
        license            contains_pii       jurisdiction
        classification     special_category

    \b
    Target references:
        @N         Job step N in the active session
        <hash>     Artifact by hash prefix

    \b
    Examples:
        roar tag add license=GPL-3.0       @1
        roar tag add contains_pii=present  @1
        roar tag rm  license=GPL-3.0       @1
        roar tag rm  license               @1
        roar tag show                      @1
        roar tag history                   @1
        roar tag why  contains_pii         model.pt
        roar tag bind                      model.pt
        roar tag unbind                    model.pt
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@tag.command("add")
@click.argument("kv")
@click.argument("target")
@click.pass_obj
@require_init
def tag_add_cmd(ctx: RoarContext, kv: str, target: str) -> None:
    """Add a value to a tag set.

    \b
    Examples:
        roar tag add license=GPL-3.0        @1
        roar tag add contains_pii=present   @2
        roar tag add jurisdiction=EU        a1b2c3d4
    """
    _warn_if_noncanonical(kv)
    try:
        rendered = tag_add(TagAddRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, kv=kv, target=target))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@tag.command("rm")
@click.argument("key_or_kv")
@click.argument("target")
@click.pass_obj
@require_init
def tag_rm_cmd(ctx: RoarContext, key_or_kv: str, target: str) -> None:
    """Remove a value (or entire kind) from a tag.

    Pass KIND=VALUE to remove one value; pass just KIND to remove the whole key.

    \b
    Examples:
        roar tag rm license=GPL-3.0  @1
        roar tag rm license          @1
    """
    try:
        rendered = tag_rm(
            TagRmRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, key_or_kv=key_or_kv, target=target)
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@tag.command("show")
@click.argument("target")
@click.pass_obj
@require_init
def tag_show_cmd(ctx: RoarContext, target: str) -> None:
    """Show current tags for a target.

    \b
    Examples:
        roar tag show @1
        roar tag show a1b2c3d4
    """
    try:
        rendered = tag_show(TagShowRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, target=target))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@tag.command("history")
@click.argument("target")
@click.pass_obj
@require_init
def tag_history_cmd(ctx: RoarContext, target: str) -> None:
    """Show all label versions for a target.

    \b
    Examples:
        roar tag history @1
        roar tag history a1b2c3d4
    """
    try:
        rendered = tag_history(TagHistoryRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, target=target))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@tag.command("bind")
@click.argument("targets", nargs=-1, required=True)
@click.pass_obj
@require_init
def tag_bind_cmd(ctx: RoarContext, targets: tuple[str, ...]) -> None:
    """Promote artifacts' current tags to cross-session scope.

    Snapshot semantics: covers exactly each artifact's tag set at bind time.
    Echoes what it promotes — path, size, and the tag set — so binding junk
    means reading past a line that says the artifact is empty.

    \b
    Examples:
        roar tag bind model.pt
        roar tag bind a1b2c3d4 e5f6a7b8
    """
    try:
        rendered = tag_bind(TagBindRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, targets=targets))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@tag.command("unbind")
@click.argument("targets", nargs=-1, required=True)
@click.pass_obj
@require_init
def tag_unbind_cmd(ctx: RoarContext, targets: tuple[str, ...]) -> None:
    """Revoke artifacts' currently-bound tags.

    Append-only revocation: writes a new event, doesn't delete the bind it
    revokes. Everything that inherited through it is mechanically
    identifiable as superseded — one unbind heals the whole cone.

    \b
    Examples:
        roar tag unbind model.pt
    """
    try:
        rendered = tag_unbind(TagUnbindRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, targets=targets))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


@tag.command("why")
@click.argument("key")
@click.argument("target")
@click.pass_obj
@require_init
def tag_why_cmd(ctx: RoarContext, key: str, target: str) -> None:
    """Explain how TARGET acquired a tag — walk the inheritance path to the human act.

    KEY is KIND or KIND=VALUE (a value narrows the explanation to one value).

    \b
    Examples:
        roar tag why contains_pii       model.pkl
        roar tag why license=GPL-3.0    @2
    """
    try:
        rendered = tag_why(
            TagWhyRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, key=key, target=target)
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(rendered)


def _warn_if_noncanonical(kv: str) -> None:
    kind = kv.split("=", 1)[0].strip()
    if kind and kind not in CANONICAL_TAG_KINDS:
        click.echo(
            f"Warning: '{kind}' is not a canonical tag kind. "
            f"Canonical kinds: {', '.join(sorted(CANONICAL_TAG_KINDS))}.",
            err=True,
        )
