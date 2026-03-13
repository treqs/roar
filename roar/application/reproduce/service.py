"""Application orchestration for artifact reproduction workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ...config import load_config
from ...core.bootstrap import bootstrap
from ...glaas_client import GlaasClient
from ...presenters.console import ConsolePresenter
from ...services.reproduction import PipelineExecutor, ReproductionService
from ...services.reproduction.pipeline_metadata import PipelineMetadataParser
from .requests import ReproduceRequest

if TYPE_CHECKING:
    from ...core.interfaces.presenter import IPresenter
    from ...core.interfaces.reproduction import PipelineInfo, ReproductionResult


def reproduce_artifact(
    request: ReproduceRequest,
    presenter: IPresenter | None = None,
) -> None:
    """Execute the `roar reproduce` application workflow."""
    bootstrap(request.roar_dir)

    if len(request.hash_prefix) < 8:
        raise ValueError("Hash prefix must be at least 8 characters for uniqueness")

    output = presenter or ConsolePresenter()
    server_url = _load_server_url(request.cwd)
    glaas_client = _build_glaas_client(server_url)

    if request.out_path:
        _write_dag_output(
            request.hash_prefix,
            request.out_path,
            glaas_client,
            output,
        )

    service = ReproductionService(
        glaas_client=glaas_client,
        presenter=output,
    )

    if not request.run_pipeline:
        _show_preview(
            service=service,
            hash_prefix=request.hash_prefix,
            server_url=server_url,
            roar_dir=request.roar_dir,
            list_requirements=request.list_requirements,
            presenter=output,
        )
        output.print("")
        output.print(
            "To reproduce this artifact (clone repo, create venv, install packages, run pipeline):"
        )
        output.print(f"  roar reproduce --run {request.hash_prefix}")
        return

    result = service.reproduce(
        hash_prefix=request.hash_prefix,
        server_url=server_url,
        run_pipeline=True,
        auto_confirm=request.auto_confirm,
        roar_dir=request.roar_dir,
        cwd=request.cwd,
        dpkg_any_version=request.dpkg_any_version,
        pip_any_version=request.pip_any_version,
        package_sync=request.package_sync,
        list_requirements=request.list_requirements,
    )
    _render_reproduction_result(result, output)


def _load_server_url(cwd: Path) -> str | None:
    config = load_config(start_dir=str(cwd) if cwd else None)
    return config.get("glaas", {}).get("url")


def _build_glaas_client(server_url: str | None) -> GlaasClient | None:
    client = GlaasClient(server_url)
    return client if client.is_configured() else None


def _write_dag_output(
    hash_prefix: str,
    out_path: str,
    glaas_client: GlaasClient | None,
    presenter: IPresenter,
) -> None:
    if not glaas_client:
        raise ValueError("--out requires a configured GLaaS server")

    dag_data, dag_error = glaas_client.get_artifact_dag(hash_prefix)
    if dag_error:
        raise ValueError(f"Failed to fetch DAG lineage: {dag_error}")

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(dag_data, handle, indent=2)
    presenter.print(f"DAG lineage response written to {out_path}")


def _show_preview(
    *,
    service: ReproductionService,
    hash_prefix: str,
    server_url: str | None,
    roar_dir: Path,
    list_requirements: bool,
    presenter: IPresenter,
) -> None:
    lookup = service.lookup_pipeline_result(hash_prefix, server_url, roar_dir)
    if lookup.error:
        raise ValueError(lookup.error)
    pipeline = lookup.pipeline
    if not pipeline:
        raise ValueError(f"No pipeline found for artifact {hash_prefix}")

    presenter.print(f"Artifact: {pipeline.artifact_hash}")
    presenter.print(f"Git repo: {pipeline.git_repo or 'Not available'}")
    presenter.print(f"Git commit: {pipeline.git_commit or 'Not available'}")

    executor = PipelineExecutor(presenter=presenter)
    executor.preview_steps(pipeline)

    _render_requirement_summary(
        pipeline=pipeline,
        list_requirements=list_requirements,
        presenter=presenter,
    )


def _render_requirement_summary(
    *,
    pipeline: PipelineInfo,
    list_requirements: bool,
    presenter: IPresenter,
) -> None:
    parser = PipelineMetadataParser()
    summary = parser.summarize_requirements(pipeline.build_steps, pipeline.run_steps)

    _print_requirement_block(
        label="Build tool packages",
        values=sorted(summary.build_dpkg),
        list_requirements=list_requirements,
        presenter=presenter,
    )
    _print_requirement_block(
        label="Build tool pip packages",
        values=sorted(summary.build_pip),
        list_requirements=list_requirements,
        presenter=presenter,
    )
    _print_requirement_block(
        label="System packages",
        values=sorted(summary.dpkg),
        list_requirements=list_requirements,
        presenter=presenter,
        suffix="  (requires sudo on Linux)",
    )
    _print_requirement_block(
        label="Pip packages",
        values=summary.pip,
        list_requirements=list_requirements,
        presenter=presenter,
    )


def _print_requirement_block(
    *,
    label: str,
    values: list[str],
    list_requirements: bool,
    presenter: IPresenter,
    suffix: str | None = None,
) -> None:
    if not values:
        return

    presenter.print(f"\n{label} ({len(values)}):")
    if list_requirements:
        for value in values:
            presenter.print(f"  - {value}")
    else:
        for value in values[:10]:
            presenter.print(f"  - {value}")
        if len(values) > 10:
            presenter.print(f"  ... and {len(values) - 10} more")
    if suffix:
        presenter.print(suffix)


def _render_reproduction_result(
    result: ReproductionResult,
    presenter: IPresenter,
) -> None:
    if not result.success:
        raise ValueError(result.error or "Reproduction failed")

    presenter.print("")
    presenter.print("=" * 50)
    presenter.print("Reproduction Complete")
    presenter.print("=" * 50)

    if result.repo_dir:
        presenter.print(f"Repository: {result.repo_dir}")

    presenter.print(f"Steps run: {result.steps_run}/{result.steps_total}")

    if result.warnings:
        presenter.print("")
        presenter.print("Warnings:")
        for warning in result.warnings:
            presenter.print(f"  - {warning}")
