"""
Native Click implementation of the reproduce command.

Usage: roar reproduce [options] <hash_prefix>
"""

from pathlib import Path

import click

from ...config import load_config
from ...core.bootstrap import bootstrap
from ...presenters.console import ConsolePresenter
from ...services.reproduction import ReproductionService
from ..context import RoarContext


@click.command("reproduce")
@click.argument("hash_prefix")
@click.option("--run", "run_pipeline", is_flag=True, help="Run the full reproduction")
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
    auto_confirm: bool,
    dpkg_any_version: bool,
    pip_any_version: bool,
    package_sync: bool,
    list_requirements: bool,
    out_path: str | None,
) -> None:
    """Reproduce an artifact from its hash.

    \b
    By default, shows a preview of what reproduction would do:
    - Artifact hash and git information
    - Build and run steps
    - Packages to install

    \b
    Use --run to perform the full reproduction:
    1. Clone the git repository at the recorded commit
    2. Create virtual environment
    3. Install recorded packages
    4. Run the pipeline steps

    \b
    Examples:
        roar reproduce abc123           # Preview reproduction
        roar reproduce abc123 --run     # Full reproduction
        roar reproduce abc123 --run -y  # Full reproduction, auto-confirm
        roar reproduce abc123 --run --package-sync  # Include system packages
    """
    # Bootstrap DI container (registers logger, etc.)
    bootstrap(ctx.roar_dir)

    # Validate hash prefix
    if len(hash_prefix) < 8:
        raise click.ClickException("Hash prefix must be at least 8 characters for uniqueness")

    # Get GLaaS client if configured
    config = load_config(start_dir=str(ctx.cwd) if ctx.cwd else None)
    server_url = config.get("glaas", {}).get("url")

    # Create GLaaS client - it will fall back to GLAAS_URL env var if no URL provided
    from ...glaas_client import GlaasClient

    _glaas_client = GlaasClient(server_url)
    glaas_client: GlaasClient | None = _glaas_client if _glaas_client.is_configured() else None

    # Dump raw DAG lineage response if --out is provided
    if out_path and glaas_client:
        import json

        dag_data, dag_error = glaas_client.get_artifact_dag(hash_prefix)
        if dag_error:
            raise click.ClickException(f"Failed to fetch DAG lineage: {dag_error}")
        with open(out_path, "w") as f:
            json.dump(dag_data, f, indent=2)
        click.echo(f"DAG lineage response written to {out_path}")
    elif out_path and not glaas_client:
        raise click.ClickException("--out requires a configured GLaaS server")

    # Create service
    presenter = ConsolePresenter()
    service = ReproductionService(
        glaas_client=glaas_client,
        presenter=presenter,
    )

    # Default behavior: show preview with copy-paste command
    if not run_pipeline:
        _show_preview(service, hash_prefix, server_url, ctx.roar_dir, list_requirements)
        click.echo("")
        click.echo(
            "To reproduce this artifact (clone repo, create venv, install packages, run pipeline):"
        )
        click.echo(f"  roar reproduce --run {hash_prefix}")
        return

    # --run: full reproduction
    result = service.reproduce(
        hash_prefix=hash_prefix,
        server_url=server_url,
        run_pipeline=run_pipeline,
        auto_confirm=auto_confirm,
        roar_dir=ctx.roar_dir,
        cwd=ctx.cwd,
        dpkg_any_version=dpkg_any_version,
        pip_any_version=pip_any_version,
        package_sync=package_sync,
        list_requirements=list_requirements,
    )

    # Show result
    if result.success:
        click.echo("")
        click.echo("=" * 50)
        click.echo("Reproduction Complete")
        click.echo("=" * 50)

        if result.repo_dir:
            click.echo(f"Repository: {result.repo_dir}")

        click.echo(f"Steps run: {result.steps_run}/{result.steps_total}")

        if result.warnings:
            click.echo("")
            click.echo("Warnings:")
            for warning in result.warnings:
                click.echo(f"  - {warning}")

    else:
        raise click.ClickException(result.error or "Reproduction failed")


def _show_preview(
    service: ReproductionService,
    hash_prefix: str,
    server_url: str | None,
    roar_dir: Path,
    list_requirements: bool = False,
) -> None:
    """Show pipeline preview without running."""
    from ...services.reproduction import PipelineExecutor

    # Look up pipeline
    pipeline, error = service._lookup_pipeline(hash_prefix, server_url, roar_dir)

    if error:
        raise click.ClickException(error)

    if not pipeline:
        raise click.ClickException(f"No pipeline found for artifact {hash_prefix}")

    # Show info
    click.echo(f"Artifact: {pipeline.artifact_hash}")
    click.echo(f"Git repo: {pipeline.git_repo or 'Not available'}")
    click.echo(f"Git commit: {pipeline.git_commit or 'Not available'}")
    click.echo("")

    # Show steps
    executor = PipelineExecutor()
    executor.preview_steps(pipeline)

    # Show package info
    import json

    build_dpkg_packages = set()
    build_pip_packages = set()
    packages = set()
    dpkg_packages = set()
    for step in pipeline.build_steps + pipeline.run_steps:
        metadata = step.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                continue

        # Format: {"packages": {"pip": {"numpy": "1.24.1"}, "dpkg": {...}, "build_dpkg": {...}}}
        pkgs_by_manager = metadata.get("packages", {})

        build_dpkg_pkgs = pkgs_by_manager.get("build_dpkg", {})
        if isinstance(build_dpkg_pkgs, dict):
            for name in build_dpkg_pkgs:
                if name:
                    build_dpkg_packages.add(name)

        build_pip_pkgs = pkgs_by_manager.get("build_pip", {})
        if isinstance(build_pip_pkgs, dict):
            for name in build_pip_pkgs:
                if name:
                    build_pip_packages.add(name)

        pip_packages = pkgs_by_manager.get("pip", {})
        if isinstance(pip_packages, dict):
            for name, version in pip_packages.items():
                if name:
                    packages.add(f"{name}=={version}" if version else name)

        dpkg_pkgs = pkgs_by_manager.get("dpkg", {})
        if isinstance(dpkg_pkgs, dict):
            for name in dpkg_pkgs:
                if name:
                    dpkg_packages.add(name)

    if build_dpkg_packages:
        click.echo(f"\nBuild tool packages ({len(build_dpkg_packages)}):")
        if list_requirements:
            for pkg in sorted(build_dpkg_packages):
                click.echo(f"  - {pkg}")
        else:
            for pkg in sorted(build_dpkg_packages)[:10]:
                click.echo(f"  - {pkg}")
            if len(build_dpkg_packages) > 10:
                click.echo(f"  ... and {len(build_dpkg_packages) - 10} more")

    if build_pip_packages:
        click.echo(f"\nBuild tool pip packages ({len(build_pip_packages)}):")
        if list_requirements:
            for pkg in sorted(build_pip_packages):
                click.echo(f"  - {pkg}")
        else:
            for pkg in sorted(build_pip_packages)[:10]:
                click.echo(f"  - {pkg}")
            if len(build_pip_packages) > 10:
                click.echo(f"  ... and {len(build_pip_packages) - 10} more")

    if dpkg_packages:
        click.echo(f"\nSystem packages ({len(dpkg_packages)}):")
        if list_requirements:
            for pkg in sorted(dpkg_packages):
                click.echo(f"  - {pkg}")
        else:
            for pkg in sorted(dpkg_packages)[:10]:
                click.echo(f"  - {pkg}")
            if len(dpkg_packages) > 10:
                click.echo(f"  ... and {len(dpkg_packages) - 10} more")
        click.echo("  (requires sudo on Linux)")

    if packages:
        click.echo(f"\nPip packages ({len(packages)}):")
        if list_requirements:
            for pkg in sorted(packages):
                click.echo(f"  - {pkg}")
        else:
            for pkg in sorted(packages)[:10]:
                click.echo(f"  - {pkg}")
            if len(packages) > 10:
                click.echo(f"  ... and {len(packages) - 10} more")
