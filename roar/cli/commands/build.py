"""
Native Click implementation of the build command.

Usage: roar build [options] <command>
"""

import click

from ...core.tracer_modes import TRACER_MODE_VALUES
from ...execution.framework.planning import plan_execution_command
from ..context import RoarContext
from ..decorators import require_init
from ._execution import (
    execute_and_report,
    get_hash_algorithms,
    get_quiet_setting,
    validate_git_clean,
)


@click.command(
    "build",
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
    help="Tracer backend policy for this build",
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
def build(
    ctx: RoarContext,
    args: tuple[str, ...],
    quiet: bool | None,
    step_name: str | None,
    tracer_mode: str | None,
    tracer_fallback: bool | None,
    hash_algorithms: tuple[str, ...],
) -> None:
    """Run a build step with provenance tracking.

    Build steps are tracked separately from run steps and run before
    DAG steps during reproduction. Use for environment setup tasks.

    \b
    Examples:
        roar build maturin develop --release
        roar build make -j4
        roar build pip install -e .
    """
    args_list = list(args)

    # Check for help
    if not args_list or args_list[0] in ("-h", "--help"):
        click.echo(_get_help_text())
        return

    # Validate git is clean
    repo_root = validate_git_clean()

    # Get quiet setting
    quiet_setting = get_quiet_setting(quiet, repo_root)

    # Get hash algorithms
    algorithms = get_hash_algorithms(list(hash_algorithms) if hash_algorithms else None)

    planned = plan_execution_command(args_list)

    # Execute and report (always job_type="build")
    exit_code = execute_and_report(
        ctx=ctx,
        backend_name=planned.backend_name,
        command=planned.command,
        job_type="build",
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
    """Get help text for the build command."""
    return """Usage: roar build [--quiet] <command> [args...]

Run a build step with provenance tracking.
Build steps run before DAG steps during reproduction.

Use for:
  - Compiling native extensions (maturin, cargo, make)
  - Installing local packages (pip install -e .)
  - Any setup that should run before the main pipeline

Options:
  --quiet, -q    Suppress output summary
  --tracer       Tracer policy: auto, ebpf, preload, ptrace
  --tracer-fallback / --no-tracer-fallback
                 Enable/disable runtime tracer fallback
  --hash <algo>  Add hash algorithm (can be repeated)
  -n, --name     Name for this step

Examples:
  roar build maturin develop --release
  roar build pip install -e .
  roar build make -j4"""
