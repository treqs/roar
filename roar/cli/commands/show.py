"""Native Click wrapper for the local show query."""

from __future__ import annotations

import click

from ...application.query.requests import ShowQueryRequest, ShowQuerySelector
from ...application.query.results import ShowArtifactSummary
from ...application.query.show import ShowQueryError, render_show_with_summary
from ..context import RoarContext
from ..decorators import require_init


@click.command("show")
@click.option("--path", "path_ref", metavar="PATH", help="Show an artifact by path.")
@click.option(
    "--job",
    "job_ref",
    metavar="REF",
    help="Show a job by step ref or UID (for example, @1 or deadbeef).",
)
@click.option("--artifact", "artifact_ref", metavar="HASH", help="Show an artifact by hash.")
@click.option("--session", "show_session", is_flag=True, help="Show the active session.")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show all items without truncation (packages, env vars, jobs, etc.).",
)
@click.argument("ref", required=False)
@click.pass_obj
@require_init
def show(
    ctx: RoarContext,
    path_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
    show_session: bool,
    show_all: bool,
    ref: str | None,
) -> None:
    """Show session, job, or artifact details.

    Without arguments, displays the active session and its jobs.
    With a reference, displays detailed information based on the reference type.
    Explicit selectors avoid ambiguous auto-detection.

    \b
    REF can be:
      - @N or @BN: Job by step number (e.g., @1, @B2)
      - 8-char hex: Job by UID
      - Longer hex: Artifact by hash (falls back to job if found)
      - File path: Artifact at that path (e.g., ./output/model.pkl)

    \b
    Examples:
        roar show                          # Show active session overview
        roar show --session                # Show active session overview explicitly
        roar show @1                       # Show details for step 1
        roar show --job @B1                # Show build step details explicitly
        roar show @B1                      # Show details for build step 1
        roar show --artifact deadbeef      # Force artifact lookup for an ambiguous hash
        roar show a1b2c3d4                 # Show job by UID
        roar show a1b2c3d4e5f67890...      # Show artifact by hash
        roar show --path deadbeef          # Force path lookup for an ambiguous filename
        roar show ./output/model.pkl       # Show artifact by path
    """
    request = _build_show_request(
        ctx=ctx,
        ref=ref,
        path_ref=path_ref,
        job_ref=job_ref,
        artifact_ref=artifact_ref,
        show_session=show_session,
        show_all=show_all,
    )
    from .._format import print_brand_header

    print_brand_header("show")
    try:
        output, summary = render_show_with_summary(request)
    except ShowQueryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(output)

    # Artifact targets get a `roar reproduce` nudge — the on-mission
    # follow-up for a hash you're staring at. Jobs and sessions don't,
    # because `roar reproduce` takes an artifact (the thing you're
    # trying to recreate), not a job or session.
    if isinstance(summary, ShowArtifactSummary):
        from .._format import hints_should_print, make_hint_printer

        if hints_should_print():
            _caps, hint = make_hint_printer()
            hint(f"To reproduce this artifact: roar reproduce {summary.id}")


def _build_show_request(
    *,
    ctx: RoarContext,
    ref: str | None,
    path_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
    show_session: bool,
    show_all: bool,
) -> ShowQueryRequest:
    explicit_targets: list[tuple[str, str | None, ShowQuerySelector]] = []
    if path_ref is not None:
        explicit_targets.append(("--path", path_ref, "path"))
    if job_ref is not None:
        explicit_targets.append(("--job", job_ref, "job"))
    if artifact_ref is not None:
        explicit_targets.append(("--artifact", artifact_ref, "artifact"))
    if show_session:
        explicit_targets.append(("--session", None, "session"))

    if len(explicit_targets) > 1:
        raise click.UsageError("Specify only one of --path, --job, --artifact, or --session.")

    if explicit_targets:
        if ref is not None:
            raise click.UsageError(
                "Positional REF cannot be combined with --path, --job, --artifact, or --session."
            )
        _, explicit_ref, selector = explicit_targets[0]
        return ShowQueryRequest(
            roar_dir=ctx.roar_dir,
            cwd=ctx.cwd,
            ref=explicit_ref,
            selector=selector,
            show_all=show_all,
        )

    return ShowQueryRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, ref=ref, show_all=show_all)
