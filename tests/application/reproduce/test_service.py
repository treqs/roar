from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.reproduce.requests import ReproduceRequest
from roar.application.reproduce.results import ReproducePreviewSummary, ReproduceRunSummary
from roar.application.reproduce.service import (
    build_preview_summary,
    build_reproduction_script,
    build_run_summary,
    reproduce_artifact,
)
from roar.core.interfaces.reproduction import (
    EnvironmentInfo,
    PipelineLookupResult,
    ReproductionResult,
)


def _request(tmp_path: Path, **overrides) -> ReproduceRequest:
    return ReproduceRequest(
        hash_prefix=overrides.pop("hash_prefix", "abc123def456"),
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        target_kind=overrides.pop("target_kind", "artifact"),
        run_pipeline=overrides.pop("run_pipeline", False),
        auto_confirm=overrides.pop("auto_confirm", False),
        dpkg_any_version=overrides.pop("dpkg_any_version", False),
        pip_any_version=overrides.pop("pip_any_version", False),
        package_sync=overrides.pop("package_sync", False),
        list_requirements=overrides.pop("list_requirements", False),
        out_path=overrides.pop("out_path", None),
        **overrides,
    )


def test_build_reproduction_script_reemits_wandb_to_trackio() -> None:
    # A run captured with --wandb-to-trackio re-emits the flag in the --script
    # output, so the reproducer's `import wandb` resolves the same way.
    pipeline = _pipeline("lineage")
    pipeline.run_steps = [
        {
            "command": "python train.py",
            "metadata": json.dumps({"run_modifiers": {"wandb_to_trackio": True}}),
        }
    ]
    script = build_reproduction_script(pipeline, "b" * 64, target_kind="lineage")
    assert "roar run --wandb-to-trackio python train.py" in script


def _pipeline(target_kind: str = "artifact") -> MagicMock:
    pipeline = MagicMock()
    pipeline.artifact_hash = "abc123def456789" if target_kind == "artifact" else ""
    pipeline.session_hash = "b" * 64 if target_kind == "lineage" else None
    pipeline.target_kind = target_kind
    pipeline.git_repo = "https://github.com/test/repo"
    pipeline.git_commit = "deadbeef"
    pipeline.build_steps = [
        {
            "command": "pip install -r requirements.txt",
            "metadata": json.dumps({"packages": {"build_pip": {"wheel": ""}}}),
        }
    ]
    pipeline.run_steps = [
        {
            "command": "python train.py",
            "metadata": json.dumps({"packages": {"pip": {"numpy": "1.26.0"}}}),
        }
    ]
    return pipeline


def test_reproduce_preview_uses_application_branching_and_renders_steps(tmp_path: Path) -> None:
    presenter = MagicMock()
    service = MagicMock()
    service.lookup_pipeline_result.return_value = PipelineLookupResult(
        pipeline=_pipeline(),
        error=None,
        source="remote",
    )

    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch(
            "roar.application.reproduce.service.load_config",
            return_value={"glaas": {"url": "http://localhost:3001"}},
        ),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch(
            "roar.application.reproduce.service.lookup_pipeline_result",
            return_value=service.lookup_pipeline_result.return_value,
        ),
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = True
        mock_glaas_cls.return_value = mock_glaas

        reproduce_artifact(_request(tmp_path), presenter=presenter)

    printed = "\n".join(call.args[0] for call in presenter.print.call_args_list)
    assert "Artifact: abc123def456789" in printed
    assert "Git repo: https://github.com/test/repo" in printed
    assert "Pipeline Preview" in printed
    assert "Build tool pip packages (1):" in printed
    assert "Pip packages (1):" in printed
    assert "roar reproduce --run abc123def456" in printed


def test_reproduce_run_executes_full_reproduction_and_renders_completion(tmp_path: Path) -> None:
    presenter = MagicMock()
    service = MagicMock()
    service.lookup_pipeline_result.return_value = PipelineLookupResult(
        pipeline=_pipeline(),
        error=None,
        source="remote",
    )
    service.prepare_environment.return_value = EnvironmentInfo(
        repo_dir=tmp_path / "reproduce" / "repo",
        venv_dir=tmp_path / "reproduce" / "repo" / ".venv",
        python_version="3.11.0",
        packages=[],
    )
    service.execute_pipeline.return_value = (2, 2)

    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch(
            "roar.application.reproduce.service.load_config",
            return_value={"glaas": {"url": "http://localhost:3001"}},
        ),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch(
            "roar.application.reproduce.service.lookup_pipeline_result",
            return_value=service.lookup_pipeline_result.return_value,
        ),
        patch(
            "roar.application.reproduce.service.prepare_reproduction_environment",
            return_value=service.prepare_environment.return_value,
        ) as mock_prepare,
        patch("roar.application.reproduce.service.PipelineExecutor") as mock_executor_cls,
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = True
        mock_glaas_cls.return_value = mock_glaas
        mock_executor = MagicMock()
        mock_executor.execute.return_value = (2, 2)
        mock_executor_cls.return_value = mock_executor

        reproduce_artifact(
            _request(tmp_path, run_pipeline=True, auto_confirm=True), presenter=presenter
        )

    mock_prepare.assert_called_once()
    mock_executor.execute.assert_called_once()
    printed = "\n".join(call.args[0] for call in presenter.print.call_args_list)
    assert "Found artifact: abc123def456789" in printed
    assert "Environment ready:" in printed
    assert "Reproduction Complete" in printed
    assert "Steps run: 2/2" in printed


def test_reproduce_out_writes_dag_response(tmp_path: Path) -> None:
    presenter = MagicMock()
    service = MagicMock()
    service.lookup_pipeline_result.return_value = PipelineLookupResult(
        pipeline=_pipeline(),
        error=None,
        source="remote",
    )
    out_path = tmp_path / "dag.json"

    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch(
            "roar.application.reproduce.service.load_config",
            return_value={"glaas": {"url": "http://localhost:3001"}},
        ),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch(
            "roar.application.reproduce.service.lookup_pipeline_result",
            return_value=service.lookup_pipeline_result.return_value,
        ),
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = True
        mock_glaas.get_artifact_dag.return_value = ({"jobs": [{"id": 1}]}, None)
        mock_glaas_cls.return_value = mock_glaas

        reproduce_artifact(_request(tmp_path, out_path=str(out_path)), presenter=presenter)

    assert json.loads(out_path.read_text()) == {"jobs": [{"id": 1}]}
    printed = "\n".join(call.args[0] for call in presenter.print.call_args_list)
    assert f"DAG lineage response written to {out_path}" in printed


def test_reproduce_out_requires_configured_glaas(tmp_path: Path) -> None:
    presenter = MagicMock()

    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch(
            "roar.application.reproduce.service.load_config", return_value={"glaas": {"url": ""}}
        ),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = False
        mock_glaas_cls.return_value = mock_glaas

        with pytest.raises(ValueError, match="--out requires a configured GLaaS server"):
            reproduce_artifact(
                _request(tmp_path, out_path=str(tmp_path / "dag.json")), presenter=presenter
            )


def test_reproduce_rejects_short_hash_prefix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 8 characters"):
        reproduce_artifact(_request(tmp_path, hash_prefix="short"), presenter=MagicMock())


def test_reproduce_rejects_non_full_lineage_hash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 64-character hexadecimal hash"):
        reproduce_artifact(
            _request(tmp_path, hash_prefix="abc123def456", target_kind="lineage"),
            presenter=MagicMock(),
        )


def test_build_preview_summary_returns_typed_preview(tmp_path: Path) -> None:
    summary = build_preview_summary(
        _pipeline(),
        hash_prefix="abc123def456",
    )

    assert isinstance(summary, ReproducePreviewSummary)
    assert summary.artifact_hash == "abc123def456789"
    assert [step.command for step in summary.build_steps] == ["pip install -r requirements.txt"]
    assert [step.command for step in summary.run_steps] == ["python train.py"]
    assert [block.label for block in summary.requirement_blocks] == [
        "Build tool packages",
        "Build tool pip packages",
        "System packages",
        "Pip packages",
    ]


def test_build_preview_summary_for_lineage_adds_flag_to_run_hint(tmp_path: Path) -> None:
    summary = build_preview_summary(
        _pipeline(target_kind="lineage"),
        hash_prefix="b" * 64,
    )

    assert isinstance(summary, ReproducePreviewSummary)
    assert summary.target_kind == "lineage"
    assert summary.session_hash == "b" * 64
    assert summary.run_hint == f"roar reproduce --run {'b' * 64} --lineage"


def test_build_run_summary_returns_typed_completion_summary() -> None:
    summary = build_run_summary(
        ReproductionResult(
            success=True,
            repo_dir=Path("/tmp/reproduce/repo"),
            steps_run=2,
            steps_total=3,
            warnings=["warn-1"],
        )
    )

    assert isinstance(summary, ReproduceRunSummary)
    assert summary.steps_run == 2
    assert summary.steps_total == 3
    assert summary.warnings == ["warn-1"]


def test_reproduce_run_skip_after_environment_renders_warning_summary(tmp_path: Path) -> None:
    presenter = MagicMock()
    presenter.confirm.side_effect = [True, False]
    service = MagicMock()
    service.lookup_pipeline_result.return_value = PipelineLookupResult(
        pipeline=_pipeline(),
        error=None,
        source="remote",
    )
    service.prepare_environment.return_value = EnvironmentInfo(
        repo_dir=tmp_path / "reproduce" / "repo",
        venv_dir=None,
        python_version=None,
        packages=[],
    )

    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch(
            "roar.application.reproduce.service.load_config",
            return_value={"glaas": {"url": "http://localhost:3001"}},
        ),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch(
            "roar.application.reproduce.service.lookup_pipeline_result",
            return_value=service.lookup_pipeline_result.return_value,
        ),
        patch(
            "roar.application.reproduce.service.prepare_reproduction_environment",
            return_value=service.prepare_environment.return_value,
        ),
        patch("roar.application.reproduce.service.PipelineExecutor") as mock_executor_cls,
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = True
        mock_glaas_cls.return_value = mock_glaas
        mock_executor = MagicMock()
        mock_executor.execute.return_value = (2, 2)
        mock_executor_cls.return_value = mock_executor

        reproduce_artifact(_request(tmp_path, run_pipeline=True), presenter=presenter)

    mock_executor.execute.assert_not_called()
    printed = "\n".join(call.args[0] for call in presenter.print.call_args_list)
    assert "Pipeline not executed (user chose to skip)" in printed


# -- Part A: a partial run must fail (no more exit-0 after tracebacks) --


def test_reproduce_run_raises_when_steps_incomplete(tmp_path: Path) -> None:
    presenter = MagicMock()
    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch(
            "roar.application.reproduce.service.load_config",
            return_value={"glaas": {"url": "http://localhost:3001"}},
        ),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch(
            "roar.application.reproduce.service.lookup_pipeline_result",
            return_value=PipelineLookupResult(pipeline=_pipeline(), error=None, source="remote"),
        ),
        patch(
            "roar.application.reproduce.service.prepare_reproduction_environment",
            return_value=EnvironmentInfo(
                repo_dir=tmp_path / "r" / "repo",
                venv_dir=tmp_path / "r" / "repo" / ".venv",
                python_version="3.11.0",
                packages=[],
            ),
        ),
        patch("roar.application.reproduce.service.PipelineExecutor") as mock_executor_cls,
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = True
        mock_glaas_cls.return_value = mock_glaas
        mock_executor = MagicMock()
        mock_executor.execute.return_value = (1, 2)  # only 1 of 2 steps completed
        mock_executor_cls.return_value = mock_executor

        with pytest.raises(ValueError, match="reproduction failed: 1/2 steps completed"):
            reproduce_artifact(
                _request(tmp_path, run_pipeline=True, auto_confirm=True), presenter=presenter
            )


# -- Part B: pre-run unsourced-input audit (warn -> exists+hash -> fail) --


def _unsourced_summary(path: str, digest: str = "a" * 64):
    from roar.application.query.results import (
        InputArtifactSummary,
        InputsSummary,
        ShowHashSummary,
    )

    return InputsSummary(
        target_ref="t",
        is_root=False,
        artifacts=[
            InputArtifactSummary(
                "art1",
                path,
                10,
                hashes=[ShowHashSummary(algorithm="blake3", digest=digest)],
                unsourced=True,
            )
        ],
    )


def test_audit_is_silent_when_unsourced_input_present_and_matching(tmp_path: Path) -> None:
    # Present-but-unsourced inputs no longer warn here — they're surfaced by the
    # reproducibility checklist instead. The audit only fails fast on broken ones.
    from roar.application.reproduce.service import _audit_unsourced_inputs

    presenter = MagicMock()
    with (
        patch(
            "roar.application.query.inputs.build_inputs_summary",
            return_value=_unsourced_summary("/w/gen.py"),
        ),
        patch("os.path.exists", return_value=True),
        patch("roar.db.hashing.backend.compute_hash", return_value="a" * 64),
    ):
        _audit_unsourced_inputs(_request(tmp_path), presenter)  # no raise

    out = "\n".join(c.args[0] for c in presenter.print.call_args_list)
    assert out == ""


def test_audit_fails_fast_when_unsourced_input_missing(tmp_path: Path) -> None:
    from roar.application.reproduce.service import _audit_unsourced_inputs

    with (
        patch(
            "roar.application.query.inputs.build_inputs_summary",
            return_value=_unsourced_summary("/w/gen.py"),
        ),
        patch("os.path.exists", return_value=False),
        pytest.raises(ValueError, match="Cannot reproduce"),
    ):
        _audit_unsourced_inputs(_request(tmp_path), MagicMock())


def test_audit_fails_fast_when_unsourced_input_changed(tmp_path: Path) -> None:
    from roar.application.reproduce.service import _audit_unsourced_inputs

    with (
        patch(
            "roar.application.query.inputs.build_inputs_summary",
            return_value=_unsourced_summary("/w/gen.py", digest="a" * 64),
        ),
        patch("os.path.exists", return_value=True),
        patch("roar.db.hashing.backend.compute_hash", return_value="b" * 64),
        pytest.raises(ValueError, match="Cannot reproduce"),
    ):
        _audit_unsourced_inputs(_request(tmp_path), MagicMock())


def test_audit_is_silent_when_target_not_locally_resolvable(tmp_path: Path) -> None:
    from roar.application.reproduce.service import _audit_unsourced_inputs

    presenter = MagicMock()
    with patch(
        "roar.application.query.inputs.build_inputs_summary",
        side_effect=RuntimeError("remote-only"),
    ):
        _audit_unsourced_inputs(_request(tmp_path), presenter)  # no raise
    presenter.print.assert_not_called()


def test_audit_noop_when_no_unsourced_inputs(tmp_path: Path) -> None:
    from roar.application.query.results import InputsSummary
    from roar.application.reproduce.service import _audit_unsourced_inputs

    presenter = MagicMock()
    with patch(
        "roar.application.query.inputs.build_inputs_summary",
        return_value=InputsSummary(target_ref="t", is_root=False, artifacts=[]),
    ):
        _audit_unsourced_inputs(_request(tmp_path), presenter)
    presenter.print.assert_not_called()


# -- reproduction runs in its own session (no lineage pollution) --


def _session_ctx(*, active, steps):
    ctx = MagicMock()
    ctx.__enter__.return_value = ctx
    ctx.__exit__.return_value = False
    ctx.sessions.get_active.return_value = active
    ctx.sessions.get_steps.return_value = steps
    return ctx


def test_reproduction_session_reuses_empty_active(tmp_path: Path) -> None:
    from roar.application.reproduce.service import _reproduction_session

    (tmp_path / ".roar").mkdir()
    (tmp_path / ".roar" / "roar.db").write_text("")
    ctx = _session_ctx(active={"id": 1}, steps=[])  # empty session

    with (
        patch("roar.db.context.create_database_context", return_value=ctx),
        _reproduction_session(tmp_path, MagicMock()),
    ):
        pass

    ctx.sessions.create.assert_not_called()  # reuse, don't churn a new one
    ctx.sessions.set_active.assert_not_called()


def test_reproduction_session_isolates_nonempty_active(tmp_path: Path) -> None:
    from roar.application.reproduce.service import _reproduction_session

    (tmp_path / ".roar").mkdir()
    (tmp_path / ".roar" / "roar.db").write_text("")
    ctx = _session_ctx(active={"id": 7}, steps=[{"id": 1}])  # has a step
    presenter = MagicMock()

    with (
        patch("roar.db.context.create_database_context", return_value=ctx),
        _reproduction_session(tmp_path, presenter),
    ):
        pass

    ctx.sessions.create.assert_called_once_with(make_active=True)  # new session
    ctx.sessions.set_active.assert_called_once_with(7)  # restored afterward
    out = "\n".join(c.args[0] for c in presenter.print.call_args_list)
    assert "new session" in out


def test_reproduction_session_noop_without_db(tmp_path: Path) -> None:
    from roar.application.reproduce.service import _reproduction_session

    # No .roar DB -> nothing to isolate, must not crash.
    with _reproduction_session(tmp_path, MagicMock()):
        pass
