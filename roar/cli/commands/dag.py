"""
Native Click implementation of the dag command.

Usage: roar dag [options]
"""

from __future__ import annotations

import json
import sys

import click

from ...db.context import create_database_context
from ...presenters.dag_data_builder import DagDataBuilder
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
    # Import here to avoid circular imports
    from ...core.models.dag import (
        DagArtifactInfo,
        DagArtifactState,
        DagNodeInfo,
        DagNodeMetrics,
        DagNodeState,
        DagVisualization,
    )
    from ...presenters.dag_renderer import DagRenderer

    with create_database_context(ctx.roar_dir) as db_ctx:
        # Get active session
        session = db_ctx.sessions.get_active()
        if not session:
            raise click.ClickException("No active session. Run 'roar init' or 'roar run' first.")

        session_id = session["id"]

        # Get DAG data
        builder = DagDataBuilder(db_ctx, session_id)
        dag_data = builder.build(
            expanded=expanded,
            show_artifacts=show_artifacts,
            stale_only=stale_only,
        )

        # Convert to Pydantic models
        nodes = [
            DagNodeInfo(
                step_number=n["step_number"],
                job_id=n["job_id"],
                job_uid=n["job_uid"],
                command=n["command"],
                step_name=n["step_name"],
                state=DagNodeState(n["state"]),
                is_build=n["is_build"],
                exit_code=n["exit_code"],
                metrics=DagNodeMetrics(**n["metrics"]),
                dependencies=n["dependencies"],
                labels=n.get("labels", {}),
            )
            for n in dag_data["nodes"]
        ]

        artifacts = [
            DagArtifactInfo(
                path=a["path"],
                hash=a["hash"],
                is_stale=a["is_stale"],
                producer_step=a["producer_step"],
                state=DagArtifactState(a["state"]),
                artifact_id=a["artifact_id"],
                consumer_steps=a["consumer_steps"],
                is_terminal=a["is_terminal"],
                superseded_by=a["superseded_by"],
                labels=a.get("labels", {}),
            )
            for a in dag_data["artifacts"]
        ]

        dag_viz = DagVisualization(
            nodes=nodes,
            artifacts=artifacts,
            stale_count=dag_data["stale_count"],
            total_steps=dag_data["total_steps"],
            is_expanded=dag_data["is_expanded"],
            session_id=dag_data["session_id"],
            stale_artifact_count=dag_data["stale_artifact_count"],
            superseded_artifact_count=dag_data["superseded_artifact_count"],
            labels=dag_data.get("labels", {}),
        )

        # Render output
        use_color = not no_color and sys.stdout.isatty()
        renderer = DagRenderer(use_color=use_color)

        if output_json:
            json_output = renderer.render_json(dag_viz)
            click.echo(json.dumps(json_output, indent=2))
        else:
            text_output = renderer.render(dag_viz)
            click.echo(text_output)
