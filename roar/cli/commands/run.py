"""
Native Click implementation of the run command.

Usage: roar run [options] <command>
       roar run @N [--param=value ...]
"""

import os

import click

from ...application.run import RunRequest, run_command
from ...application.tags import parse_tag_kv
from ...core.tracer_modes import TRACER_MODE_VALUES
from .._tag_kinds import enforce_tag_kind
from ..context import RoarContext
from ..decorators import require_init


def _validate_add_tags(
    ctx: click.Context, param: click.Parameter, value: tuple[str, ...]
) -> tuple[str, ...]:
    for item in value:
        try:
            kind, _value = parse_tag_kv(item)
        except ValueError as exc:
            raise click.BadParameter(str(exc)) from exc
        enforce_tag_kind(kind)
    return value


@click.command(
    "run",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "allow_interspersed_args": False,
    },
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=None,
    help="Silent mode (only program output + exit code).",
)
@click.option(
    "-v",
    "--verbose",
    "verbose",
    count=True,
    help="Increase verbosity. -v lists read/written files; -vv also lists filtered files.",
)
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
@click.option(
    "--block-tag",
    "block_tags",
    multiple=True,
    metavar="KIND[=VALUE]",
    help=(
        "Stop a compliance tag from being inherited for this run (repeatable). "
        "KIND blocks the whole kind; KIND=VALUE filters just that value "
        "(e.g. license=GPL-3.0 for a relicensing step)."
    ),
)
@click.option(
    "--add-tag",
    "add_tags",
    multiple=True,
    metavar="KIND=VALUE",
    callback=_validate_add_tags,
    help="Stamp KIND=VALUE onto this run's output artifacts (repeatable).",
)
@click.option(
    "--capture-trackio",
    is_flag=True,
    help="Route the workload's W&B / MLflow logging into trackio (zero code change), "
    "so it syncs to a hosted HF Space whose URL is recorded on the DAG.",
)
@click.option(
    "--trackio-space",
    default=None,
    metavar="OWNER/NAME",
    help="HF Space id to sync trackio runs to (e.g. reproducible-ai/experiments); "
    "used with --capture-trackio.",
)
@click.pass_obj
@require_init
def run(
    ctx: RoarContext,
    args: tuple[str, ...],
    quiet: bool | None,
    verbose: int,
    step_name: str | None,
    tracer_mode: str | None,
    tracer_fallback: bool | None,
    hash_algorithms: tuple[str, ...],
    block_tags: tuple[str, ...],
    add_tags: tuple[str, ...],
    capture_trackio: bool,
    trackio_space: str | None,
) -> None:
    """Run a command with provenance tracking.

    Automatically tracks input files (read), output files (written),
    command exit code, duration, and git commit.

    \b
    Inside a git repo, requires a clean working tree: every run is tagged
    with the current commit SHA so artifacts trace back to the exact code
    that produced them. Commit your changes before running. Outside a repo,
    runs are still captured — just without a commit (and so can't be
    registered for reproducible sharing until run inside a repo).

    \b
    Examples:
        roar run python train.py
        roar run ./scripts/preprocess.sh
        roar run @2                    # Re-run DAG node 2
        roar run @2 --epochs=10        # Re-run with parameter override
    """
    # Trackio capture: set the env the injected sitecustomize reads. The traced
    # child inherits os.environ (tracer builds env = dict(os.environ)), so the
    # wandb/mlflow -> trackio shim fires and the analyzer records the Space URL.
    if capture_trackio:
        os.environ["ROAR_CAPTURE_TRACKIO"] = "1"
        if trackio_space:
            os.environ["ROAR_TRACKIO_SPACE_ID"] = trackio_space

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
                cli_verbose=verbose,
                step_name=step_name,
                tracer_mode=tracer_mode,
                tracer_fallback=tracer_fallback,
                hash_algorithms=tuple(hash_algorithms),
                block_tags=tuple(block_tags),
                add_tags=tuple(add_tags),
            )
        )
    except ValueError as exc:
        if str(exc) == "No command specified":
            click.echo(_get_help_text())
        elif _is_dirty_tree_error(exc):
            from ...telemetry.hooks import record_run_outcome

            record_run_outcome(success=False, failure_kind="dirty", start_dir=ctx.cwd)
        raise click.ClickException(str(exc)) from exc

    if exit_code != 0:
        raise SystemExit(exit_code)


def _is_dirty_tree_error(exc: ValueError) -> bool:
    message = str(exc)
    return message.startswith("Run blocked:") and (
        "working tree is dirty" in message or "home directory" in message
    )


def _get_help_text() -> str:
    """Get help text for the run command."""
    return """Usage: roar run [options] <command> [args...]
       roar run @N [--param=value ...]   # Re-run DAG node N
       roar run @BN [--param=value ...]  # Re-run build node N

Run a command with provenance tracking.

Options:
  --quiet, -q             Silent mode (only program output + exit code)
  -v, -vv                 Verbose / debug output (-v lists I/O; -vv also lists filtered files)
  --tracer <mode>         Tracer policy: auto, ebpf, preload, ptrace
  --tracer-fallback       Enable runtime tracer fallback
  --no-tracer-fallback    Disable runtime tracer fallback
  --hash <algo>           Add hash algorithm (can be repeated)
  -n, --name <name>       Set the name label for this step
  --block-tag <kind[=value]>  Stop a tag kind (or one value) from being inherited (repeatable)
  --add-tag <kind=value>  Stamp a tag onto this run's outputs (repeatable)

Hash algorithms: blake3 (default), sha256, sha512, md5

Note: inside a git repo, `roar run` requires a clean working tree and tags
each run with the current commit SHA so artifacts trace to specific code.
Outside a repo it still captures lineage, just without a commit.

Examples:
  roar run python train.py
  roar run @2 --epochs=10    # Re-run step 2 with parameter override"""
