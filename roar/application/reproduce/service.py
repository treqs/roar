"""Application orchestration for artifact reproduction workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.bootstrap import bootstrap
from ...integrations.config import load_config
from ...integrations.glaas import GlaasClient
from ...presenters.console import ConsolePresenter
from ...services.reproduction import ReproductionService
from ...services.reproduction.pipeline_metadata import PipelineMetadataParser
from .requests import ReproduceRequest
from .results import (
    ReproducePreviewStepSummary,
    ReproducePreviewSummary,
    ReproduceRequirementBlock,
    ReproduceRunSummary,
)

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

    pipeline = _resolve_pipeline(
        service=service,
        hash_prefix=request.hash_prefix,
        server_url=server_url,
        roar_dir=request.roar_dir,
    )
    preview = build_preview_summary(
        pipeline,
        hash_prefix=request.hash_prefix,
    )

    if not request.run_pipeline:
        _render_preview_summary(
            preview,
            presenter=output,
            list_requirements=request.list_requirements,
        )
        output.print("")
        output.print(
            "To reproduce this artifact (clone repo, create venv, install packages, run pipeline):"
        )
        output.print(f"  {preview.run_hint}")
        return

    if not pipeline.git_repo:
        raise ValueError("Cannot reproduce: no git repository URL available")

    output.print(f"Found artifact: {pipeline.artifact_hash}")
    output.print(f"Git repo: {pipeline.git_repo or 'Not available'}")
    output.print(f"Git commit: {pipeline.git_commit or 'Not available'}")
    output.print(f"Build steps: {len(pipeline.build_steps)}")
    output.print(f"Run steps: {len(pipeline.run_steps)}")

    if request.list_requirements:
        for block in preview.requirement_blocks:
            _print_requirement_block(
                label=block.label,
                values=block.values,
                list_requirements=True,
                presenter=output,
                suffix=block.suffix,
            )

    if not request.auto_confirm and not output.confirm("Proceed with reproduction?", default=True):
        raise ValueError("Reproduction cancelled by user")

    try:
        environment = service.prepare_environment(
            pipeline,
            request.cwd,
            request.auto_confirm,
            dpkg_any_version=request.dpkg_any_version,
            pip_any_version=request.pip_any_version,
            package_sync=request.package_sync,
        )
    except RuntimeError as exc:
        raise ValueError(f"Environment setup failed: {exc}") from exc

    output.print(f"Environment ready: {environment.repo_dir}")
    _render_pipeline_steps(preview, presenter=output)

    if not request.auto_confirm and not output.confirm("Run the pipeline?", default=True):
        _render_reproduction_result(
            ReproduceRunSummary(
                repo_dir=environment.repo_dir,
                steps_run=0,
                steps_total=pipeline.total_steps,
                warnings=["Pipeline not executed (user chose to skip)"],
            ),
            output,
        )
        return

    steps_run, steps_total = service.execute_pipeline(
        pipeline,
        environment,
        request.auto_confirm,
    )
    _render_reproduction_result(
        ReproduceRunSummary(
            repo_dir=environment.repo_dir,
            steps_run=steps_run,
            steps_total=steps_total,
            warnings=[],
        ),
        output,
    )


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


def _resolve_pipeline(
    *,
    service: ReproductionService,
    hash_prefix: str,
    server_url: str | None,
    roar_dir: Path,
) -> PipelineInfo:
    """Resolve the target pipeline for reproduction."""
    lookup = service.lookup_pipeline_result(hash_prefix, server_url, roar_dir)
    if lookup.error:
        raise ValueError(lookup.error)
    pipeline = lookup.pipeline
    if not pipeline:
        raise ValueError(f"No pipeline found for artifact {hash_prefix}")
    return pipeline


def build_preview_summary(
    pipeline: PipelineInfo,
    *,
    hash_prefix: str,
) -> ReproducePreviewSummary:
    """Build a typed reproduction preview summary."""
    requirement_blocks = _build_requirement_blocks(pipeline)
    return ReproducePreviewSummary(
        artifact_hash=pipeline.artifact_hash,
        git_repo=pipeline.git_repo,
        git_commit=pipeline.git_commit,
        run_hint=f"roar reproduce --run {hash_prefix}",
        build_steps=[
            ReproducePreviewStepSummary(
                phase="build",
                index=index,
                command=str(step.get("command", "No command")),
            )
            for index, step in enumerate(pipeline.build_steps, 1)
        ],
        run_steps=[
            ReproducePreviewStepSummary(
                phase="run",
                index=index,
                command=str(step.get("command", "No command")),
            )
            for index, step in enumerate(pipeline.run_steps, 1)
        ],
        requirement_blocks=requirement_blocks,
    )


def _render_preview_summary(
    summary: ReproducePreviewSummary,
    *,
    list_requirements: bool,
    presenter: IPresenter,
) -> None:
    presenter.print(f"Artifact: {summary.artifact_hash}")
    presenter.print(f"Git repo: {summary.git_repo or 'Not available'}")
    presenter.print(f"Git commit: {summary.git_commit or 'Not available'}")
    presenter.print("\nPipeline Preview")
    presenter.print("=" * 40)

    if summary.build_steps:
        presenter.print(f"\nBuild Steps ({len(summary.build_steps)}):")
        for step in summary.build_steps:
            presenter.print(f"  B{step.index}. {step.command}")

    if summary.run_steps:
        presenter.print(f"\nRun Steps ({len(summary.run_steps)}):")
        for step in summary.run_steps:
            presenter.print(f"  {step.index}. {step.command}")

    presenter.print("")

    for block in summary.requirement_blocks:
        _print_requirement_block(
            label=block.label,
            values=block.values,
            list_requirements=list_requirements,
            presenter=presenter,
            suffix=block.suffix,
        )


def _build_requirement_blocks(pipeline: PipelineInfo) -> list[ReproduceRequirementBlock]:
    parser = PipelineMetadataParser()
    summary = parser.summarize_requirements(pipeline.build_steps, pipeline.run_steps)
    return [
        ReproduceRequirementBlock(
            label="Build tool packages",
            values=sorted(summary.build_dpkg),
        ),
        ReproduceRequirementBlock(
            label="Build tool pip packages",
            values=sorted(summary.build_pip),
        ),
        ReproduceRequirementBlock(
            label="System packages",
            values=sorted(summary.dpkg),
            suffix="  (requires sudo on Linux)",
        ),
        ReproduceRequirementBlock(
            label="Pip packages",
            values=summary.pip,
        ),
    ]


def _render_pipeline_steps(
    summary: ReproducePreviewSummary,
    *,
    presenter: IPresenter,
) -> None:
    presenter.print("\nPipeline Preview")
    presenter.print("=" * 40)

    if summary.build_steps:
        presenter.print(f"\nBuild Steps ({len(summary.build_steps)}):")
        for step in summary.build_steps:
            presenter.print(f"  B{step.index}. {step.command}")

    if summary.run_steps:
        presenter.print(f"\nRun Steps ({len(summary.run_steps)}):")
        for step in summary.run_steps:
            presenter.print(f"  {step.index}. {step.command}")

    presenter.print("")


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
    result: ReproduceRunSummary,
    presenter: IPresenter,
) -> None:
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


def build_run_summary(result: ReproductionResult) -> ReproduceRunSummary:
    """Build a typed reproduction-run summary from the service result."""
    if not result.success:
        raise ValueError(result.error or "Reproduction failed")
    return ReproduceRunSummary(
        repo_dir=result.repo_dir,
        steps_run=result.steps_run,
        steps_total=result.steps_total,
        warnings=list(result.warnings),
    )
