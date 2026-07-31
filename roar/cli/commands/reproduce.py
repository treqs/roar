"""
Native Click implementation of the reproduce command.

Usage: roar reproduce [options] <hash_prefix>
"""

import click

from ...application.reproduce import ReproduceRequest, reproduce_artifact
from ..context import RoarContext


@click.command("reproduce")
@click.argument("hash_prefix")
@click.option("--run", "run_pipeline", is_flag=True, help="Run the full reproduction")
@click.option(
    "--script",
    "emit_script",
    is_flag=True,
    help="Emit an editable reproduction shell script (clone, env, pipeline) without executing",
)
@click.option(
    "--no-puts",
    "no_puts",
    is_flag=True,
    help="Skip publish (roar put) steps — rebuild the artifact without re-publishing",
)
@click.option(
    "--lineage",
    "reproduce_lineage",
    is_flag=True,
    help="Interpret HASH as a full 64-character lineage/session hash",
)
@click.option("-y", "--yes", "auto_confirm", is_flag=True, help="Auto-confirm all prompts")
@click.option(
    "--dpkg-any-version",
    is_flag=True,
    help="Install any available version of dpkg packages when exact version not found",
)
@click.option(
    "--pip-any-version",
    is_flag=True,
    help="Install any available version of pip packages when exact version not found",
)
@click.option(
    "--package-sync",
    is_flag=True,
    help="Install OS system packages (build_dpkg and dpkg) during environment setup",
)
@click.option(
    "--list-requirements",
    is_flag=True,
    help="Show all build tool, pip, and dpkg packages that will be installed (no truncation)",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(),
    default=None,
    help="Dump DAG lineage response to a JSON file",
)
@click.pass_obj
def reproduce(
    ctx: RoarContext,
    hash_prefix: str,
    run_pipeline: bool,
    emit_script: bool,
    no_puts: bool,
    reproduce_lineage: bool,
    auto_confirm: bool,
    dpkg_any_version: bool,
    pip_any_version: bool,
    package_sync: bool,
    list_requirements: bool,
    out_path: str | None,
) -> None:
    """Reproduce an artifact or lineage from a recorded hash.

    \b
    By default, HASH is treated as an artifact hash. Use --lineage to reproduce
    a full recorded lineage/session by its 64-character lineage hash.

    \b
    Preview shows:
    - target hash and git information
    - build and run steps
    - packages to install

    \b
    Use --run to perform the full reproduction:
    1. Clone the git repository at the recorded commit
    2. Create virtual environment
    3. Install recorded packages
    4. Run the pipeline steps

    \b
    Examples:
        roar reproduce abc123                          # Preview artifact reproduction
        roar reproduce abc123 --run                    # Run artifact reproduction
        roar reproduce <lineage-hash> --lineage        # Preview lineage reproduction
        roar reproduce <lineage-hash> --lineage --run  # Run lineage reproduction
        roar reproduce abc123 --run --package-sync     # Include system packages
    """
    try:
        reproduce_artifact(
            ReproduceRequest(
                hash_prefix=hash_prefix,
                roar_dir=ctx.roar_dir,
                cwd=ctx.cwd,
                target_kind="lineage" if reproduce_lineage else "artifact",
                run_pipeline=run_pipeline,
                emit_script=emit_script,
                no_puts=no_puts,
                auto_confirm=auto_confirm,
                dpkg_any_version=dpkg_any_version,
                pip_any_version=pip_any_version,
                package_sync=package_sync,
                list_requirements=list_requirements,
                out_path=out_path,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
