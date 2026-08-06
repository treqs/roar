"""Application orchestration for artifact reproduction workflows."""

from __future__ import annotations

import json
import re
import shlex
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ...core.bootstrap import bootstrap
from ...execution.reproduction.pipeline_executor import PipelineExecutor
from ...execution.reproduction.pipeline_metadata import PipelineMetadataParser
from ...integrations.config import load_config
from ...integrations.glaas import GlaasClient
from ...presenters.console import ConsolePresenter
from .environment import prepare_reproduction_environment
from .lookup import lookup_pipeline_result
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

    _validate_target_hash(request)

    output = presenter or ConsolePresenter()
    server_url = _load_server_url(request.cwd)
    glaas_client = _build_glaas_client(server_url)

    if request.out_path and not request.emit_script:
        _write_reproduction_output(
            request.hash_prefix,
            request.target_kind,
            request.out_path,
            glaas_client,
            output,
        )

    if request.emit_script and request.run_pipeline:
        raise ValueError("choose either --script or --run, not both")

    pipeline, _pipeline_source = _resolve_pipeline(
        hash_prefix=request.hash_prefix,
        server_url=server_url,
        roar_dir=request.roar_dir,
        glaas_client=glaas_client,
        target_kind=request.target_kind,
    )

    # --no-puts: drop publish (`roar put`) steps so a third-party reproduction
    # rebuilds the artifact without re-publishing to the owner's destination.
    # Applied to the pipeline itself so preview, --script, and --run all agree
    # (and the steps_run/steps_total accounting stays consistent).
    if request.no_puts:
        dropped = _drop_put_steps(pipeline)
        # In --script mode stdout is the script itself; the omission is documented
        # by a comment inside it, so don't pollute stdout with a notice here.
        if dropped and not request.emit_script:
            output.print(f"Skipping {dropped} publish (roar put) step(s) [--no-puts]")

    # --script: emit an editable reproduction script instead of previewing/running.
    if request.emit_script:
        output.print(
            build_reproduction_script(
                pipeline,
                request.hash_prefix,
                target_kind=request.target_kind,
                no_puts=request.no_puts,
            )
        )
        return

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
            f"To reproduce this {_target_label(request.target_kind).lower()} (clone repo, create venv, install packages, run pipeline):"
        )
        output.print(f"  {preview.run_hint}")
        return

    output.print(
        f"Found {_target_label(pipeline.target_kind).lower()}: {_pipeline_target_hash(pipeline)}"
    )
    output.print(f"Git repo: {pipeline.git_repo or 'Not available'}")
    output.print(f"Git commit: {pipeline.git_commit or 'Not available'}")
    output.print(f"Build steps: {len(pipeline.build_steps)}")
    output.print(f"Run steps: {len(pipeline.run_steps)}")

    # Fail fast (before the clone) if the lineage depends on inputs nothing
    # tracked produced and that are missing or changed here.
    _audit_unsourced_inputs(request, output)

    # Show the recorded lineage's reproducibility checklist before running, so the
    # user knows what they're getting (and why a result might diverge).
    output.print("")
    output.print(_render_reproducibility_checklist(request, pipeline))

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
        environment = prepare_reproduction_environment(
            pipeline=pipeline,
            cwd=request.cwd,
            presenter=output,
            auto_confirm=request.auto_confirm,
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

    with _reproduction_session(environment.repo_dir, output):
        steps_run, steps_total = PipelineExecutor(
            presenter=output, step_timeout=request.step_timeout
        ).execute(
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
    # A reproduction that didn't run every step is a failure — surface it as a
    # non-zero exit instead of silently returning 0 after printing tracebacks.
    if steps_run < steps_total:
        raise ValueError(
            f"reproduction failed: {steps_run}/{steps_total} steps completed "
            "(see the step output above)"
        )


def _file_matches_recorded_hash(path: str, hashes: list) -> bool:
    """True iff the file at ``path`` blake3-hashes to its recorded digest.

    Conservative: if there's no recorded blake3 to compare against, or hashing
    fails, treat it as a non-match (we can't confirm the input is intact).
    """
    from ...db.hashing.backend import compute_hash

    for h in hashes:
        if h.algorithm == "blake3":
            try:
                return compute_hash(path, "blake3") == h.digest
            except Exception:
                return False
    return False


@contextmanager
def _reproduction_session(repo_dir: Path, output: IPresenter):
    """Run the replay in its own session so it never mutates the original lineage.

    Reproduction re-runs each step with roar (recording a fresh lineage you can
    compare to the original). In place, those inner runs would otherwise append
    to the repo's *active* session and pollute it. So: if the active session is
    empty, reuse it (no churn); if it has steps, warn and start a new session for
    the replay, restoring the original active session afterward. Best-effort —
    any failure here just falls back to the existing session.
    """
    roar_dir = repo_dir / ".roar"
    if not (roar_dir / "roar.db").exists():
        yield
        return

    from ...db.context import create_database_context

    restore_id: int | None = None
    try:
        with create_database_context(roar_dir) as ctx:
            active = ctx.sessions.get_active()
            if active is not None and ctx.sessions.get_steps(active["id"]):
                restore_id = int(active["id"])
                output.print(
                    "Active session already has steps — reproducing into a new session so it "
                    "doesn't add to your current lineage."
                )
                ctx.sessions.create(make_active=True)
            # empty / no active session -> reuse it (the replay fills it).
    except Exception:
        yield
        return

    try:
        yield
    finally:
        if restore_id is not None:
            try:
                with create_database_context(roar_dir) as ctx:
                    ctx.sessions.set_active(restore_id)
            except Exception:
                pass


def _render_reproducibility_checklist(request: ReproduceRequest, pipeline: PipelineInfo) -> str:
    """Build the recorded lineage's reproducibility checklist (see report module).

    Operational punchlist for "can this reproduction run": code committed, single
    commit, commit reachable on a remote (to clone), inputs sourced, runtime
    captured. The register/put receipt checks (``lineage saved on glaas.ai`` and
    ``all artifact paths in tracked directories``) are deliberately omitted —
    you already hold the lineage and are re-creating its outputs, so neither
    bears on whether the reproduction succeeds."""
    from ..reproducibility.report import (
        build_report,
        is_shareable_remote,
        render_report,
        runtime_captured,
        unsourced_input_paths,
    )

    report = build_report(
        committed=bool(pipeline.git_commit),
        pushed=is_shareable_remote(pipeline.git_repo),
        runtime_ok=runtime_captured(pipeline),
        unsourced_paths=unsourced_input_paths(request.roar_dir, request.cwd, request.hash_prefix),
        single_commit=pipeline.single_commit,
    )
    return render_report(report, title="Reproducibility (as recorded)")


def _audit_unsourced_inputs(request: ReproduceRequest, output: IPresenter) -> None:
    """Pre-flight the lineage for inputs nothing tracked produced.

    Such inputs (a pre-existing data file, a script run outside a git repo)
    won't exist after a fresh clone, so the reproduction would crash mid-run.
    Per the agreed policy:
      - present locally with the recorded hash  -> warn, then proceed
      - missing or changed                      -> fail fast (before the clone)
    Best-effort: if the target can't be resolved against the local DB (e.g. a
    remote-only lineage), skip silently — Part A still catches a failed run.
    """
    import os

    try:
        from ..query.inputs import build_inputs_summary
        from ..query.requests import InputsQueryRequest

        summary = build_inputs_summary(
            InputsQueryRequest(
                roar_dir=request.roar_dir,
                cwd=request.cwd,
                ref=request.hash_prefix,
                unsourced=True,
            )
        )
    except Exception:
        return

    if not summary.artifacts:
        return

    broken: list[str] = []
    for art in summary.artifacts:
        path = art.path
        if not (path and os.path.exists(path) and _file_matches_recorded_hash(path, art.hashes)):
            broken.append(path or art.artifact_id)

    if broken:
        listed = "\n  ".join(broken)
        raise ValueError(
            "Cannot reproduce — these inputs weren't produced by any tracked run and "
            f"are missing or changed here:\n  {listed}\n"
            "Ingest them with `roar get` / `roar run wget`, or commit code to a git repo."
        )
    # Inputs that are present-but-unsourced are surfaced by the reproducibility
    # checklist (rendered next), so this audit only fails fast on broken ones.


def _load_server_url(cwd: Path) -> str | None:
    config = load_config(start_dir=str(cwd) if cwd else None)
    return config.get("glaas", {}).get("url")


def _build_glaas_client(server_url: str | None) -> GlaasClient | None:
    client = GlaasClient(server_url)
    return client if client.is_configured() else None


def _write_reproduction_output(
    hash_prefix: str,
    target_kind: str,
    out_path: str,
    glaas_client: GlaasClient | None,
    presenter: IPresenter,
) -> None:
    if not glaas_client:
        raise ValueError("--out requires a configured GLaaS server")

    if target_kind == "lineage":
        response_data, response_error = glaas_client.get_session_reproduction(hash_prefix)
        if response_error:
            raise ValueError(f"Failed to fetch session reproduction payload: {response_error}")
        success_message = f"Session reproduction response written to {out_path}"
    else:
        response_data, response_error = glaas_client.get_artifact_dag(hash_prefix)
        if response_error:
            raise ValueError(f"Failed to fetch DAG lineage: {response_error}")
        success_message = f"DAG lineage response written to {out_path}"

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(response_data, handle, indent=2)
    presenter.print(success_message)


def _resolve_pipeline(
    *,
    hash_prefix: str,
    server_url: str | None,
    roar_dir: Path,
    glaas_client: GlaasClient | None,
    target_kind: Literal["artifact", "lineage"],
) -> tuple[PipelineInfo, str]:
    """Resolve the target pipeline for reproduction; returns (pipeline, source)."""
    lookup = lookup_pipeline_result(
        hash_prefix=hash_prefix,
        roar_dir=roar_dir,
        server_url=server_url,
        glaas_client=glaas_client,
        target_kind=target_kind,
    )
    if lookup.error:
        raise ValueError(lookup.error)
    pipeline = lookup.pipeline
    if not pipeline:
        raise ValueError(
            f"No pipeline found for {_target_label(target_kind).lower()} {hash_prefix}"
        )
    return pipeline, lookup.source


def _is_put_step(step: dict) -> bool:
    """A publish step: recorded as job_type 'put' or a `roar put …` command."""
    kind = step.get("job_type") or step.get("jobType")
    if isinstance(kind, str) and kind.strip().lower() == "put":
        return True
    cmd = str(step.get("command") or "").strip()
    return cmd.split()[:2] == ["roar", "put"]


def _drop_put_steps(pipeline: PipelineInfo) -> int:
    """Remove publish steps from the pipeline in place; return how many were dropped."""
    before = len(pipeline.run_steps)
    pipeline.run_steps = [s for s in pipeline.run_steps if not _is_put_step(s)]
    pipeline.total_steps = len(pipeline.build_steps) + len(pipeline.run_steps)
    return before - len(pipeline.run_steps)


def build_reproduction_script(
    pipeline: PipelineInfo,
    hash_prefix: str,
    *,
    target_kind: str = "artifact",
    no_puts: bool = False,
) -> str:
    """Render an editable bash reproduction script (clone → env → pipeline).

    Dry by design: nothing runs until the user executes it. Mirrors what
    `--run` would do, so it composes with `--no-puts` (which has already
    filtered publish steps out of *pipeline*).
    """
    from ..tags import run_modifier_flags

    lines = [
        "#!/usr/bin/env bash",
        f"# Reproduction script for {target_kind} {hash_prefix}",
        "# Generated by `roar reproduce --script`. Dry & editable: review the steps",
        "# (e.g. change --device, or run on your own infra) before executing.",
    ]
    if no_puts:
        lines.append("# Publish (roar put) steps are omitted (--no-puts).")
    lines += ["set -euo pipefail", ""]

    repo = pipeline.git_repo or "<unknown-git-repo>"
    lines += [
        "# 1. Clone the recorded code at its exact commit",
        f"git clone {shlex.quote(repo)} reproduction",
        "cd reproduction",
    ]
    if pipeline.git_commit:
        lines.append(f"git checkout {shlex.quote(pipeline.git_commit)}")
    if not pipeline.single_commit:
        lines.append("# NOTE: the recorded session spanned multiple commits; results may differ.")
    lines.append("")

    lines += [
        "# 2. Recreate the Python environment",
        "python3 -m venv .venv",
        "source .venv/bin/activate",
    ]
    for block in _build_requirement_blocks(pipeline):
        if not block.values:
            continue
        if block.label in ("Pip packages", "Build tool pip packages"):
            lines.append("pip install " + " ".join(shlex.quote(v) for v in block.values))
        else:  # system / build-tool dpkg packages need sudo — emit as a comment
            lines.append(f"# {block.label} (apt; requires sudo): " + " ".join(block.values))
    lines.append("")

    lines.append("# 3. Run the pipeline (recorded order)")
    for step in pipeline.build_steps:
        cmd = str(step.get("command") or "").strip()
        if cmd:
            lines.append(f"roar build {cmd}")
    for step in pipeline.run_steps:
        cmd = str(step.get("command") or "").strip()
        if not cmd:
            continue
        # A step whose command is already a `roar …` invocation (e.g. `roar put`)
        # is emitted as-is; other commands are wrapped in `roar run` (replaying
        # any recorded run-modifiers) exactly as the executor would.
        if cmd.split()[:1] == ["roar"]:
            lines.append(cmd)
            continue
        metadata = step.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}
        mods = (
            run_modifier_flags(metadata.get("run_modifiers")) if isinstance(metadata, dict) else ""
        )
        prefix = f"roar run {mods}".strip()
        lines.append(f"{prefix} {cmd}")
    lines.append("")
    return "\n".join(lines)


def build_preview_summary(
    pipeline: PipelineInfo,
    *,
    hash_prefix: str,
) -> ReproducePreviewSummary:
    """Build a typed reproduction preview summary."""
    requirement_blocks = _build_requirement_blocks(pipeline)
    run_hint = f"roar reproduce --run {hash_prefix}"
    if pipeline.target_kind == "lineage":
        run_hint = f"{run_hint} --lineage"

    return ReproducePreviewSummary(
        artifact_hash=pipeline.artifact_hash,
        git_repo=pipeline.git_repo,
        git_commit=pipeline.git_commit,
        run_hint=run_hint,
        target_kind=pipeline.target_kind,
        session_hash=pipeline.session_hash,
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
    presenter.print(f"{_target_label(summary.target_kind)}: {_summary_target_hash(summary)}")
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


def _validate_target_hash(request: ReproduceRequest) -> None:
    if request.target_kind == "lineage":
        if not re.fullmatch(r"[0-9a-fA-F]{64}", request.hash_prefix):
            raise ValueError("Lineage hash must be a full 64-character hexadecimal hash")
        return

    if len(request.hash_prefix) < 8:
        raise ValueError("Hash prefix must be at least 8 characters for uniqueness")


def _target_label(target_kind: str) -> str:
    return "Lineage" if target_kind == "lineage" else "Artifact"


def _pipeline_target_hash(pipeline: PipelineInfo) -> str:
    if pipeline.target_kind == "lineage":
        return pipeline.session_hash or ""
    return pipeline.artifact_hash


def _summary_target_hash(summary: ReproducePreviewSummary) -> str:
    if summary.target_kind == "lineage":
        return summary.session_hash or ""
    return summary.artifact_hash


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
