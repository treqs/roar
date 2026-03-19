"""
Native Click implementation of the run command.

Usage: roar run [options] <command>
       roar run @N [--param=value ...]
"""

import click

from ...application.run import RunRequest, run_command
from ...core.tracer_modes import TRACER_MODE_VALUES
from ..context import RoarContext
from ..decorators import require_init


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
@click.option("-n", "--name", "step_name", help="Set the name label for this step")
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
def run(
    ctx: RoarContext,
    args: tuple[str, ...],
    quiet: bool | None,
    step_name: str | None,
    tracer_mode: str | None,
    tracer_fallback: bool | None,
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

    try:
        exit_code = run_command(
            RunRequest(
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                args=tuple(args_list),
                quiet=quiet,
                step_name=step_name,
                tracer_mode=tracer_mode,
                tracer_fallback=tracer_fallback,
                hash_algorithms=tuple(hash_algorithms),
            )
        )
    except ValueError as exc:
        if str(exc) == "No command specified":
            click.echo(_get_help_text())
        raise click.ClickException(str(exc)) from exc

    if exit_code != 0:
        raise SystemExit(exit_code)


def _get_help_text() -> str:
    """Get help text for the run command."""
    return """Usage: roar run [options] <command> [args...]
       roar run @N [--param=value ...]   # Re-run DAG node N
       roar run @BN [--param=value ...]  # Re-run build node N

Run a command with provenance tracking.

Options:
  --quiet, -q             Suppress output summary
  --tracer <mode>         Tracer policy: auto, ebpf, preload, ptrace
  --tracer-fallback       Enable runtime tracer fallback
  --no-tracer-fallback    Disable runtime tracer fallback
  --hash <algo>           Add hash algorithm (can be repeated)
  -n, --name <name>       Set the name label for this step

Hash algorithms: blake3 (default), sha256, sha512, md5

Examples:
  roar run python train.py
  roar run @2 --epochs=10    # Re-run step 2 with parameter override"""
