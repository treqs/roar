"""Native Click wrapper for the roar diff command."""

from __future__ import annotations

from typing import cast

import click

from ...application.query.diff import DiffError, render_diff
from ...application.query.requests import DiffFormat, DiffQueryRequest
from ..context import RoarContext
from ..decorators import require_init


@click.command("diff")
@click.argument("ref_a")
@click.argument("ref_b")
@click.option("--json", "output_json", is_flag=True, help="Machine-readable JSON output.")
@click.option("--depth", type=int, default=None, help="Max lineage traversal depth.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["summary", "category", "dag"]),
    default="summary",
    help="Output format.",
)
@click.option(
    "--analyze", is_flag=True, help="Add LLM-assisted analysis (requires ANTHROPIC_API_KEY)."
)
@click.pass_obj
@require_init
def diff(
    ctx: RoarContext,
    ref_a: str,
    ref_b: str,
    output_json: bool,
    depth: int | None,
    fmt: str,
    analyze: bool,
) -> None:
    """Compare the provenance of two artifacts, jobs, or steps.

    Walks the upstream lineage DAG for each reference and reports what
    differs: data inputs, parameters, code, environment, or pipeline
    structure. Diffs are ranked by impact score to surface root causes.

    \b
    REF can be:
      - File path:      ./model.pkl, ./output/metrics.json
      - Artifact hash:  b61ba24d...
      - Step ref:       @5, @7
      - Job UID:        017a0de0
      - Session:        session:current, session:<hash>
      - Remote (GLaaS): glaas:<hash>

    \b
    Examples:
        roar diff ./model.pkl ./model2.pkl
        roar diff @5 @7
        roar diff @5 @7 --format dag
        roar diff @5 @7 --format category
        roar diff session:current session:<hash>
        roar diff glaas:<hash> ./model.pkl
        roar diff @5 @7 --analyze
        roar diff @5 @7 --json
    """
    request = DiffQueryRequest(
        roar_dir=ctx.roar_dir,
        cwd=ctx.cwd,
        ref_a=ref_a,
        ref_b=ref_b,
        output_json=output_json,
        depth=depth,
        format=cast(DiffFormat, fmt),
        analyze=analyze,
    )
    try:
        click.echo(render_diff(request))
    except DiffError as exc:
        raise click.ClickException(str(exc)) from exc
