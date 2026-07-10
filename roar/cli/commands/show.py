"""Native Click wrapper for the local show query."""

from __future__ import annotations

import click

from ...application.query.requests import ShowQueryRequest, ShowQuerySelector
from ...application.query.results import ShowArtifactSummary
from ...application.query.show import ShowQueryError, render_show_with_summary
from ..context import RoarContext
from ..decorators import ensure_initialized

_REMOTE_HELP = (
    "Look up the artifact on GLaaS if it isn't found locally "
    "(labels, owner/scope, producing job); no local project required."
)


def _primary_artifact_hash(summary: ShowArtifactSummary) -> str | None:
    """The artifact's primary content-hash digest for the reproduce hint.

    Prefers blake3 (roar's default and what the show header leads with), else
    the first recorded hash. Returns ``None`` when no content hash is recorded
    so the caller can fall back to the internal id.
    """
    hashes = summary.hashes or []
    for entry in hashes:
        if entry.algorithm == "blake3":
            return entry.digest
    return hashes[0].digest if hashes else None


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
@click.option("--remote", is_flag=True, help=_REMOTE_HELP)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show all items without truncation (packages, env vars, jobs, etc.).",
)
@click.argument("ref", required=False)
@click.pass_obj
def show(
    ctx: RoarContext,
    path_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
    show_session: bool,
    remote: bool,
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
        roar show <hash> --remote          # Look up a published artifact on GLaaS,
                                            # even with no local .roar project
    """
    if not remote:
        ensure_initialized(ctx)
    request = _build_show_request(
        ctx=ctx,
        ref=ref,
        path_ref=path_ref,
        job_ref=job_ref,
        artifact_ref=artifact_ref,
        show_session=show_session,
        remote=remote,
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
            # Echo the artifact's content hash (the value shown in the header
            # above), not the internal DB id — `roar reproduce` resolves by
            # artifact hash, and a second, unlabeled id read to users as a
            # different/ambiguous hash.
            repro_ref = _primary_artifact_hash(summary) or summary.id
            hint(f"To reproduce this artifact: roar reproduce {repro_ref}")


def _build_show_request(
    *,
    ctx: RoarContext,
    ref: str | None,
    path_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
    show_session: bool,
    remote: bool,
    show_all: bool,
) -> ShowQueryRequest:
    if remote and (path_ref is not None or job_ref is not None or show_session):
        raise click.UsageError("--remote only supports artifact lookups.")

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
            force_remote=remote,
        )

    if remote:
        # No local project to disambiguate against, and there's no "active
        # session" concept on GLaaS — a hash is required, and it's always
        # an artifact lookup.
        if ref is None:
            raise click.UsageError("REF (an artifact hash) is required with --remote.")
        return ShowQueryRequest(
            roar_dir=ctx.roar_dir,
            cwd=ctx.cwd,
            ref=ref,
            selector="artifact",
            show_all=show_all,
            force_remote=True,
        )

    return ShowQueryRequest(roar_dir=ctx.roar_dir, cwd=ctx.cwd, ref=ref, show_all=show_all)
