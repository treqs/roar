"""
Native Click implementation of the build command.

Usage: roar build [options] <command>
"""

import click

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
@click.option("--hash", "hash_algorithms", multiple=True, help="Add hash algorithm")
@click.pass_obj
@require_init
def build(
    ctx: RoarContext,
    args: tuple[str, ...],
    quiet: bool | None,
    step_name: str | None,
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
    repo_root, git_info = validate_git_clean()

    # Get quiet setting
    quiet_setting = get_quiet_setting(quiet, repo_root)

    # Get hash algorithms
    algorithms = get_hash_algorithms(list(hash_algorithms) if hash_algorithms else None)

    # Execute and report (always job_type="build")
    exit_code = execute_and_report(
        ctx=ctx,
        command=args_list,
        job_type="build",
        quiet=quiet_setting,
        hash_algorithms=algorithms,
        git_info=git_info,
        repo_root=repo_root,
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
  --hash <algo>  Add hash algorithm (can be repeated)
  -n, --name     Name for this step

Examples:
  roar build maturin develop --release
  roar build pip install -e .
  roar build make -j4"""
