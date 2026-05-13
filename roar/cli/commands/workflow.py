"""TReqs workflow command group."""

from __future__ import annotations

from pathlib import Path

import click

from ...application.workflow import (
    GenerateWorkflowRequest,
    WorkflowGenerationError,
    generate_workflow,
)
from ..context import RoarContext
from ..decorators import require_init


@click.group("workflow", invoke_without_command=True)
@click.pass_context
def workflow(ctx: click.Context) -> None:
    """Generate TReqs workflow YAML from local roar sessions."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@workflow.command("generate")
@click.argument(
    "output_path",
    required=False,
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option(
    "--session",
    "session_ref",
    default="current",
    show_default=True,
    help="Active session or session-hash prefix shown by roar status/register.",
)
@click.option(
    "--name",
    "workflow_name",
    default=None,
    help="Override the generated TReqs workflow name.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite the output file if it already exists.",
)
@click.pass_obj
@require_init
def workflow_generate(
    ctx: RoarContext,
    output_path: Path | None,
    session_ref: str,
    workflow_name: str | None,
    force: bool,
) -> None:
    """Generate a TReqs workflow from a local session.

    Writes workflow YAML compatible with TReqs workflow discovery
    under ``.treqs/workflows`` by default.

    \b
    Examples:
        roar workflow generate
        roar workflow generate .treqs/workflows/train.yaml
        roar workflow generate --session 8d7a1f2c --name baseline-train
    """
    try:
        result = generate_workflow(
            GenerateWorkflowRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                output_path=output_path,
                session_ref=session_ref,
                workflow_name=workflow_name,
                force=force,
            )
        )
    except WorkflowGenerationError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Generated TReqs workflow: {result.display_path}")
    click.echo(f"Workflow name: {result.workflow_name}")
    click.echo(f"Source DAG: {result.session_hash}")
    if result.working_directory:
        click.echo(f"Working directory: {result.working_directory}")
    click.echo(f"Tasks: {len(result.tasks)}")
    for task in result.tasks:
        click.echo(f"  {task.step_ref:<6} {task.task_name}")


__all__ = ["workflow", "workflow_generate"]
