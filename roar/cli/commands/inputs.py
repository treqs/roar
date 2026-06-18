"""Native Click wrapper for the local inputs query."""

from __future__ import annotations

import click

from ...application.query.inputs import InputsQueryError, render_inputs
from ...application.query.requests import InputsQueryRequest, InputsQuerySelector
from ..context import RoarContext
from ..decorators import require_init


@click.command("inputs")
@click.option("--path", "path_ref", metavar="PATH", help="Look up target by file path.")
@click.option(
    "--job",
    "job_ref",
    metavar="REF",
    help="Look up target by job step ref or UID (e.g., @1 or deadbeef).",
)
@click.option("--artifact", "artifact_ref", metavar="HASH", help="Look up target by hash.")
@click.option("--direct", is_flag=True, help="Show only immediate inputs (no transitive walk).")
@click.option(
    "--all", "show_all", is_flag=True, help="Show all upstream artifacts including intermediates."
)
@click.option(
    "--unsourced",
    is_flag=True,
    help="Show only unsourced inputs (nothing tracked produced them — not reproducible).",
)
@click.option(
    "--sourced",
    is_flag=True,
    help="Show only sourced inputs (produced by a tracked run or ingested via roar get).",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON.")
@click.argument("ref", required=False)
@click.pass_obj
@require_init
def inputs(
    ctx: RoarContext,
    path_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
    direct: bool,
    show_all: bool,
    unsourced: bool,
    sourced: bool,
    output_json: bool,
    ref: str | None,
) -> None:
    """Show root input artifacts for a target.

    Walks the DAG backwards from the target and returns all root input
    artifacts -- files that were not produced by any tracked job.

    \b
    REF can be:
      - @N or @BN: Job by step number (e.g., @1, @B2)
      - 8-char hex: Job by UID
      - Longer hex: Artifact by hash
      - File path: Artifact at that path (e.g., ./model.pkl)

    \b
    Examples:
        roar inputs model.pkl                  # Root inputs for an artifact
        roar inputs @5                         # Root inputs for a job's outputs
        roar inputs --direct model.pkl         # Immediate inputs only
        roar inputs --all model.pkl            # All upstream artifacts
        roar inputs --json model.pkl           # JSON output
    """
    if direct and show_all:
        raise click.UsageError("--direct and --all are mutually exclusive.")
    if unsourced and sourced:
        raise click.UsageError("--unsourced and --sourced are mutually exclusive.")

    resolved_ref, selector = _build_ref_and_selector(
        ref=ref,
        path_ref=path_ref,
        job_ref=job_ref,
        artifact_ref=artifact_ref,
    )

    request = InputsQueryRequest(
        roar_dir=ctx.roar_dir,
        cwd=ctx.cwd,
        ref=resolved_ref,
        selector=selector,
        direct=direct,
        show_all=show_all,
        unsourced=unsourced,
        sourced=sourced,
        output_json=output_json,
    )
    try:
        click.echo(render_inputs(request))
    except InputsQueryError as exc:
        raise click.ClickException(str(exc)) from exc


def _build_ref_and_selector(
    *,
    ref: str | None,
    path_ref: str | None,
    job_ref: str | None,
    artifact_ref: str | None,
) -> tuple[str, InputsQuerySelector]:
    explicit_targets: list[tuple[str, str | None, InputsQuerySelector]] = []
    if path_ref is not None:
        explicit_targets.append(("--path", path_ref, "path"))
    if job_ref is not None:
        explicit_targets.append(("--job", job_ref, "job"))
    if artifact_ref is not None:
        explicit_targets.append(("--artifact", artifact_ref, "artifact"))

    if len(explicit_targets) > 1:
        raise click.UsageError("Specify only one of --path, --job, or --artifact.")

    if explicit_targets:
        if ref is not None:
            raise click.UsageError(
                "Positional REF cannot be combined with --path, --job, or --artifact."
            )
        _, explicit_ref, selector = explicit_targets[0]
        if explicit_ref is None:
            raise click.UsageError("A reference value is required.")
        return explicit_ref, selector

    if ref is None:
        raise click.UsageError("A target reference is required.")

    return ref, "auto"
