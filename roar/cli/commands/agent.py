"""
Native Click implementation of the agent command.

Usage: roar agent [options] <command>
"""

import click

from ...core.tracer_modes import TRACER_MODE_VALUES
from ..context import RoarContext
from ..decorators import require_init
from ._execution import (
    execute_and_report,
    get_git_root_optional,
    get_hash_algorithms,
    get_quiet_setting,
)


@click.command(
    "agent",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "allow_interspersed_args": False,
    },
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("-q", "--quiet", is_flag=True, default=None, help="Suppress output summary")
@click.option("-n", "--name", "step_name", help="Name for this step")
@click.option(
    "--tracer",
    "tracer_mode",
    type=click.Choice(list(TRACER_MODE_VALUES)),
    default=None,
    help="Tracer backend policy for this run",
)
@click.option(
    "--tracer-fallback/--no-tracer-fallback",
    "tracer_fallback",
    default=None,
    help="Allow runtime fallback to another tracer backend",
)
@click.option("--hash", "hash_algorithms", multiple=True, help="Add hash algorithm")
@click.pass_obj
@require_init
def agent(
    ctx: RoarContext,
    args: tuple[str, ...],
    quiet: bool | None,
    step_name: str | None,
    tracer_mode: str | None,
    tracer_fallback: bool | None,
    hash_algorithms: tuple[str, ...],
) -> None:
    """Run an agent with provenance tracking.

    Like 'roar run' but does not require a clean git working tree.
    Tracks all file I/O performed by the agent and its subprocesses.

    \\b
    Examples:
        roar agent codex
        roar agent bash ./my-agent-script.sh
        roar agent python my_agent.py
    """
    args_list = list(args)

    if not args_list or args_list[0] in ("-h", "--help"):
        click.echo(_get_help_text())
        return

    repo_root = get_git_root_optional()
    quiet_setting = get_quiet_setting(quiet, repo_root)
    algorithms = get_hash_algorithms(list(hash_algorithms) if hash_algorithms else None)

    command = args_list
    if not command:
        click.echo(_get_help_text())
        raise click.ClickException("No command specified")

    exit_code = execute_and_report(
        ctx=ctx,
        command=command,
        job_type="agent",
        step_name=step_name,
        quiet=quiet_setting,
        hash_algorithms=algorithms,
        repo_root=repo_root,
        tracer_mode=tracer_mode,
        tracer_fallback=tracer_fallback,
    )

    if exit_code != 0:
        raise SystemExit(exit_code)


def _get_help_text() -> str:
    """Get help text for the agent command."""
    return """Usage: roar agent [options] <command> [args...]

Run an agent with provenance tracking.

Unlike 'roar run', this does not require a clean git working tree.
The agent and all its subprocesses are traced for file I/O.

Options:
  --quiet, -q             Suppress output summary
  --tracer <mode>         Tracer policy: auto, ebpf, preload, ptrace
  --tracer-fallback       Enable runtime tracer fallback
  --no-tracer-fallback    Disable runtime tracer fallback
  --hash <algo>           Add hash algorithm (can be repeated)
  -n, --name <name>       Name for this step

Examples:
  roar agent codex
  roar agent bash ./my-agent-script.sh
  roar agent python my_agent.py"""
