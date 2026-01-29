"""
Native Click implementation of the run command.

Usage: roar run [options] <command>
       roar run @N [--param=value ...]
"""

import shlex

import click

from ...db.context import create_database_context
from ...presenters.console import ConsolePresenter
from ...presenters.run_report import RunReportPresenter
from ...services.execution import DAGReferenceResolver
from ..context import RoarContext
from ..decorators import require_init
from ._execution import (
    execute_and_report,
    get_hash_algorithms,
    get_quiet_setting,
    validate_git_clean,
)


@click.command(
    "run",
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
def run(
    ctx: RoarContext,
    args: tuple[str, ...],
    quiet: bool | None,
    step_name: str | None,
    hash_algorithms: tuple[str, ...],
) -> None:
    """Run a command with provenance tracking.

    Automatically tracks input files (read), output files (written),
    command exit code, duration, and git commit.

    \b
    Examples:
        roar run python train.py
        roar run ./scripts/preprocess.sh
        roar run @2                    # Re-run DAG node 2
        roar run @2 --epochs=10        # Re-run with parameter override
    """
    args_list = list(args)

    # Check for help
    if not args_list or args_list[0] in ("-h", "--help"):
        click.echo(_get_help_text())
        return

    # Parse out any roar-specific options from args
    command: list[str] = []
    dag_reference: str | None = None
    param_overrides: dict[str, str] = {}

    i = 0
    while i < len(args_list):
        arg = args_list[i]

        # Check for DAG reference
        if arg.startswith("@") and dag_reference is None:
            dag_reference = arg
            i += 1
            continue

        # Check for parameter overrides (only after DAG reference)
        if dag_reference and arg.startswith("--") and "=" in arg:
            key, value = arg[2:].split("=", 1)
            param_overrides[key] = value
            i += 1
            continue

        # Regular argument
        command.append(arg)
        i += 1

    # Validate git is clean
    repo_root, git_info = validate_git_clean()

    # Get quiet setting
    quiet_setting = get_quiet_setting(quiet, repo_root)

    # Get hash algorithms
    algorithms = get_hash_algorithms(list(hash_algorithms) if hash_algorithms else None)

    # Handle DAG reference
    if dag_reference:
        resolved_command, is_build = _resolve_dag_reference(
            ctx, dag_reference, param_overrides, repo_root
        )
        if resolved_command is None:
            return  # Error already printed

        command = shlex.split(resolved_command)
        job_type = "build" if is_build else None
    else:
        if not command:
            click.echo(_get_help_text())
            raise click.ClickException("No command specified")
        job_type = None

    # Execute and report
    exit_code = execute_and_report(
        ctx=ctx,
        command=command,
        job_type=job_type,
        quiet=quiet_setting,
        hash_algorithms=algorithms,
        git_info=git_info,
        repo_root=repo_root,
    )

    if exit_code != 0:
        raise SystemExit(exit_code)


def _resolve_dag_reference(
    ctx: RoarContext,
    reference: str,
    param_overrides: dict[str, str],
    repo_root: str,
) -> tuple[str | None, bool]:
    """
    Resolve @N or @BN reference to a command.

    Returns:
        Tuple of (command_string, is_build) or (None, False) on error
    """
    with create_database_context(ctx.roar_dir) as db_ctx:
        resolver = DAGReferenceResolver(
            db_ctx.sessions,
            db_ctx.jobs,
            db_ctx.artifacts,
            db_ctx.lineage,
            db_ctx.session_service,
        )
        resolved, error = resolver.resolve(reference, param_overrides)

        if error:
            raise click.ClickException(error)

        if resolved is None:
            raise click.ClickException(f"Could not resolve DAG reference: {reference}")

        # Check for stale upstream and warn
        if resolved.stale_upstream:
            presenter = ConsolePresenter()
            report = RunReportPresenter(presenter)
            if not report.show_upstream_stale_warning(
                resolved.step_number, resolved.stale_upstream
            ):
                click.echo("Aborted.")
                return None, False
            click.echo("")

        click.echo(f"Re-running @{resolved.step_number}: {resolved.command}")
        click.echo("")

        return resolved.command, resolved.is_build


def _get_help_text() -> str:
    """Get help text for the run command."""
    return """Usage: roar run [options] <command> [args...]
       roar run @N [--param=value ...]   # Re-run DAG node N
       roar run @BN [--param=value ...]  # Re-run build node N

Run a command with provenance tracking.

Options:
  --quiet, -q             Suppress output summary
  --hash <algo>           Add hash algorithm (can be repeated)
  -n, --name <name>       Name for this step

Hash algorithms: blake3 (default), sha256, sha512, md5

Examples:
  roar run python train.py
  roar run @2 --epochs=10    # Re-run step 2 with parameter override"""
