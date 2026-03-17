from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import click

from ...backends.osmo import (
    OsmoAttachOptions,
    attach_osmo_workflow,
    build_osmo_runtime_bundle,
    export_osmo_lineage_bundle,
    prepare_osmo_workflow_for_lineage,
    resolve_roar_install_requirement,
)
from ...backends.osmo.config import normalize_osmo_backend_config
from ..context import RoarContext
from ..decorators import require_init


@click.group("osmo", invoke_without_command=True)
@click.pass_context
def osmo(ctx: click.Context) -> None:
    """Manage OSMO workflow-native lineage operations."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@osmo.command("attach")
@click.argument("workflow_id")
@click.option(
    "--workflow-spec",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Optional local workflow spec used to resolve task names and declared outputs",
)
@click.option(
    "--set-string",
    "set_strings",
    multiple=True,
    help="Template replacement in KEY=VALUE form for workflow-spec placeholders",
)
@click.option(
    "--dataset",
    "datasets",
    multiple=True,
    help="Declared output dataset name to download when no local workflow spec is available",
)
@click.option(
    "--task",
    "tasks",
    multiple=True,
    help="Workflow task name to use for diagnostics/log capture when no local workflow spec is available",
)
@click.option(
    "--wait/--no-wait",
    default=None,
    help="Override osmo.wait_for_completion for this attach",
)
@click.option(
    "--download-declared-outputs/--no-download-declared-outputs",
    default=None,
    help="Override osmo.download_declared_outputs for this attach",
)
@click.option(
    "--ingest-lineage-bundles/--no-ingest-lineage-bundles",
    default=None,
    help="Override osmo.ingest_lineage_bundles for this attach",
)
@click.option(
    "--osmo-binary",
    default="osmo",
    show_default=True,
    help="OSMO CLI binary or path to invoke for workflow query/download operations",
)
@click.pass_obj
@require_init
def osmo_attach(
    ctx: RoarContext,
    workflow_id: str,
    workflow_spec: Path | None,
    set_strings: tuple[str, ...],
    datasets: tuple[str, ...],
    tasks: tuple[str, ...],
    wait: bool | None,
    download_declared_outputs: bool | None,
    ingest_lineage_bundles: bool | None,
    osmo_binary: str,
) -> None:
    """Attach local Roar lineage ingestion to an existing OSMO workflow."""
    repo_root = str(ctx.repo_root or ctx.roar_dir.parent)
    resolved_set_strings = _parse_set_strings(set_strings)

    result = attach_osmo_workflow(
        roar_dir=ctx.roar_dir,
        repo_root=repo_root,
        workflow_id=workflow_id,
        options=OsmoAttachOptions(
            workflow_spec_argument=str(workflow_spec) if workflow_spec is not None else None,
            workflow_spec_path=str(workflow_spec) if workflow_spec is not None else None,
            set_strings=resolved_set_strings or None,
            dataset_names=[item for item in datasets if item.strip()] or None,
            task_names=[item for item in tasks if item.strip()] or None,
            wait_for_completion=wait,
            download_declared_outputs=download_declared_outputs,
            ingest_lineage_bundles=ingest_lineage_bundles,
        ),
        osmo_binary=osmo_binary,
    )
    if result.exit_code != 0:
        raise SystemExit(result.exit_code)


@osmo.command("prepare-workflow")
@click.argument(
    "input_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.argument(
    "output_path",
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option(
    "--task",
    "tasks",
    multiple=True,
    help="Workflow task name to augment with the standard Roar lineage dataset output",
)
@click.option(
    "--inject-runtime-wrapper/--no-inject-runtime-wrapper",
    default=False,
    help="Rewrite selected tasks to run through a Roar wrapper that exports the standard lineage bundle",
)
@click.option(
    "--wrapper-script-path",
    default="/tmp/roar-osmo-wrapper.sh",
    show_default=True,
    help="Path written into selected OSMO tasks for the generated Roar wrapper script",
)
@click.option(
    "--stage-roar-runtime/--no-stage-roar-runtime",
    default=False,
    help="Build and attach a local Roar runtime bundle for the generated wrapper to unpack remotely",
)
@click.option(
    "--install-roar-runtime/--no-install-roar-runtime",
    default=False,
    help="Install roar-cli inside the generated wrapper at task startup instead of relying on a prebuilt image",
)
@click.option(
    "--runtime-install-requirement",
    default=None,
    help="Pinned requirement, wheel URL, or package reference installed by the generated wrapper; use a packaged roar-cli distribution with bundled tracer binaries",
)
@click.option(
    "--runtime-install-local-path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Local wheel or artifact path injected into the prepared workflow and installed by the wrapper; a roar-cli wheel should include bundled tracer binaries",
)
@click.option(
    "--runtime-install-remote-path",
    default="/tmp/roar-osmo-install.whl",
    show_default=True,
    help="Remote path used inside selected OSMO tasks for an injected runtime install artifact",
)
@click.option(
    "--runtime-bundle-path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Optional output path for the staged Roar runtime bundle tarball",
)
@click.option(
    "--runtime-python-root",
    "runtime_python_roots",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    multiple=True,
    help="Override Python runtime root(s) copied into the staged Roar runtime bundle",
)
@click.option(
    "--runtime-roar-package-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=None,
    help="Override the local roar package directory copied into the staged runtime bundle",
)
@click.option(
    "--runtime-tracer",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Override the ptrace tracer binary copied into the staged runtime bundle",
)
@click.option(
    "--runtime-bundle-remote-path",
    default="/tmp/roar-osmo-runtime.tar.gz",
    show_default=True,
    help="Remote path used inside selected OSMO tasks for the staged runtime bundle tarball",
)
@click.pass_obj
@require_init
def osmo_prepare_workflow(
    ctx: RoarContext,
    input_path: Path,
    output_path: Path,
    tasks: tuple[str, ...],
    inject_runtime_wrapper: bool,
    wrapper_script_path: str,
    stage_roar_runtime: bool,
    install_roar_runtime: bool,
    runtime_install_requirement: str | None,
    runtime_install_local_path: Path | None,
    runtime_install_remote_path: str,
    runtime_bundle_path: Path | None,
    runtime_python_roots: tuple[Path, ...],
    runtime_roar_package_dir: Path | None,
    runtime_tracer: Path | None,
    runtime_bundle_remote_path: str,
) -> None:
    """Write an OSMO workflow spec augmented with Roar's lineage dataset output."""
    if stage_roar_runtime and not inject_runtime_wrapper:
        raise click.ClickException("--stage-roar-runtime requires --inject-runtime-wrapper")
    if install_roar_runtime and not inject_runtime_wrapper:
        raise click.ClickException("--install-roar-runtime requires --inject-runtime-wrapper")
    if stage_roar_runtime and install_roar_runtime:
        raise click.ClickException(
            "--stage-roar-runtime and --install-roar-runtime are mutually exclusive"
        )
    if runtime_install_local_path is not None and not install_roar_runtime:
        raise click.ClickException("--runtime-install-local-path requires --install-roar-runtime")
    if runtime_install_local_path is not None and runtime_install_requirement is not None:
        raise click.ClickException(
            "--runtime-install-local-path and --runtime-install-requirement are mutually exclusive"
        )

    config_section = ctx.config.get("osmo", {})
    if not isinstance(config_section, Mapping):
        config_section = None
    config = normalize_osmo_backend_config(config_section)
    resolved_runtime_bundle_path: Path | None = None
    runtime_bundle_local_path: str | None = None
    if stage_roar_runtime:
        resolved_runtime_bundle_path = runtime_bundle_path or (
            output_path.parent / "roar-osmo-runtime.tar.gz"
        )
        try:
            build_osmo_runtime_bundle(
                output_path=resolved_runtime_bundle_path,
                roar_package_dir=runtime_roar_package_dir,
                python_roots=list(runtime_python_roots) or None,
                ptrace_tracer_path=runtime_tracer,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        runtime_bundle_local_path = os.path.relpath(
            resolved_runtime_bundle_path,
            start=output_path.parent,
        )

    resolved_runtime_install_requirement: str | None = None
    resolved_runtime_install_local_path: str | None = None
    if install_roar_runtime:
        if runtime_install_local_path is not None:
            resolved_runtime_install_local_path = os.path.relpath(
                runtime_install_local_path,
                start=output_path.parent,
            )
        else:
            resolved_runtime_install_requirement = resolve_roar_install_requirement(
                runtime_install_requirement
            )

    try:
        prepared = prepare_osmo_workflow_for_lineage(
            input_path=input_path,
            output_path=output_path,
            lineage_dataset_name=str(config["lineage_bundle_dataset_name"]),
            lineage_bundle_filename=str(config["lineage_bundle_filename"]),
            task_names=[item for item in tasks if item.strip()] or None,
            inject_runtime_wrapper=inject_runtime_wrapper,
            wrapper_script_path=wrapper_script_path,
            runtime_bundle_local_path=runtime_bundle_local_path,
            runtime_bundle_remote_path=runtime_bundle_remote_path,
            runtime_install_requirement=resolved_runtime_install_requirement,
            runtime_install_local_path=resolved_runtime_install_local_path,
            runtime_install_remote_path=runtime_install_remote_path,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    selected_tasks = ", ".join(prepared.selected_tasks)
    modified_tasks = ", ".join(prepared.modified_tasks) if prepared.modified_tasks else "none"
    click.echo(f"Prepared OSMO workflow: {prepared.output_path}")
    click.echo(f"Selected tasks: {selected_tasks}")
    click.echo(f"Modified tasks: {modified_tasks}")
    if prepared.wrapped_tasks:
        click.echo(f"Wrapped tasks: {', '.join(prepared.wrapped_tasks)}")
    if resolved_runtime_bundle_path is not None:
        click.echo(f"Staged runtime bundle: {resolved_runtime_bundle_path}")
    if resolved_runtime_install_requirement is not None:
        click.echo(f"Runtime install requirement: {resolved_runtime_install_requirement}")
    if resolved_runtime_install_local_path is not None:
        click.echo(f"Runtime install artifact: {resolved_runtime_install_local_path}")


@osmo.command("export-lineage-bundle")
@click.argument(
    "output_path",
    type=click.Path(path_type=Path, dir_okay=False),
)
@click.option("--job-uid", default=None, help="Specific local Roar job UID to export")
@click.option("--task-id", default=None, help="OSMO task identifier to place in the bundle")
@click.option("--task-name", default=None, help="OSMO task name to place in the bundle")
@click.pass_obj
@require_init
def osmo_export_lineage_bundle(
    ctx: RoarContext,
    output_path: Path,
    job_uid: str | None,
    task_id: str | None,
    task_name: str | None,
) -> None:
    """Export a local Roar job as an OSMO lineage bundle."""
    try:
        exported = export_osmo_lineage_bundle(
            roar_dir=ctx.roar_dir,
            output_path=output_path,
            job_uid=job_uid,
            task_id=task_id,
            task_name=task_name,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Exported OSMO lineage bundle: {exported.output_path}")
    click.echo(f"Job UID: {exported.exported_job_uid or '<none>'}")
    click.echo(f"Task ID: {exported.task_id}")
    click.echo(f"Task name: {exported.task_name}")


def _parse_set_strings(values: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise click.ClickException(f"Invalid --set-string value {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise click.ClickException(f"Invalid --set-string value {item!r}; expected KEY=VALUE")
        parsed[key] = value
    return parsed


__all__ = [
    "osmo",
    "osmo_attach",
    "osmo_export_lineage_bundle",
    "osmo_prepare_workflow",
]
