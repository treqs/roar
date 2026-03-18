"""Native Click wrapper for the local DAG query."""

from __future__ import annotations

import sys

import click

from ...application.query.dag import render_dag
from ...application.query.requests import DagQueryRequest
from ..context import RoarContext
from ..decorators import require_init


@click.command("dag")
@click.option(
    "--expanded",
    is_flag=True,
    default=False,
    help="Show full execution history with all reruns",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output machine-readable JSON",
)
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="Plain text output (no ANSI colors)",
)
@click.option(
    "--show-artifacts",
    is_flag=True,
    default=False,
    help="Show intermediate artifacts between steps (default: terminal only)",
)
@click.option(
    "--stale-only",
    is_flag=True,
    default=False,
    help="Filter to show only stale steps and artifacts",
)
@click.pass_obj
@require_init
def dag(
    ctx: RoarContext,
    expanded: bool,
    output_json: bool,
    no_color: bool,
    show_artifacts: bool,
    stale_only: bool,
) -> None:
    """Display the pipeline DAG for the current session.

    Shows all steps in the current session as a directed acyclic graph (DAG),
    with their dependencies, states, and I/O metrics.

    \b
    Each node shows:
    - Step reference (@N for run steps, @BN for build steps)
    - Command or step name
    - Metrics: in (inputs read), out (outputs written), cons (consumed from prior steps)
    - State marker: * for stale steps

    \b
    Artifact states:
    - active: Produced by active step, on the execution path
    - stale: Produced by stale step
    - superseded: Old version replaced by re-run
    - orphaned: Not consumed by any active step

    \b
    Examples:

        roar dag                  # Compact view with colors

        roar dag --expanded       # Show all executions including reruns

        roar dag --json           # Machine-readable JSON output

        roar dag --no-color       # Plain text for piping

        roar dag --show-artifacts # Show intermediate artifacts

        roar dag --stale-only     # Filter to only stale steps/artifacts
    """
    try:
        output = render_dag(
            DagQueryRequest(
                roar_dir=ctx.roar_dir,
                expanded=expanded,
                output_json=output_json,
                use_color=not no_color and sys.stdout.isatty(),
                show_artifacts=show_artifacts,
                stale_only=stale_only,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(output)
