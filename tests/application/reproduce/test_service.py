from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.reproduce.requests import ReproduceRequest
from roar.application.reproduce.service import reproduce_artifact
from roar.core.interfaces.reproduction import PipelineLookupResult, ReproductionResult


def _request(tmp_path: Path, **overrides) -> ReproduceRequest:
    return ReproduceRequest(
        hash_prefix=overrides.pop("hash_prefix", "abc123def456"),
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        run_pipeline=overrides.pop("run_pipeline", False),
        auto_confirm=overrides.pop("auto_confirm", False),
        dpkg_any_version=overrides.pop("dpkg_any_version", False),
        pip_any_version=overrides.pop("pip_any_version", False),
        package_sync=overrides.pop("package_sync", False),
        list_requirements=overrides.pop("list_requirements", False),
        out_path=overrides.pop("out_path", None),
        **overrides,
    )


def _pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.artifact_hash = "abc123def456789"
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
    executor = MagicMock()

    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch("roar.application.reproduce.service.load_config", return_value={"glaas": {"url": "http://localhost:3001"}}),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch("roar.application.reproduce.service.ReproductionService", return_value=service),
        patch("roar.application.reproduce.service.PipelineExecutor", return_value=executor),
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = True
        mock_glaas_cls.return_value = mock_glaas

        reproduce_artifact(_request(tmp_path), presenter=presenter)

    service.reproduce.assert_not_called()
    executor.preview_steps.assert_called_once()
    printed = "\n".join(call.args[0] for call in presenter.print.call_args_list)
    assert "Artifact: abc123def456789" in printed
    assert "Git repo: https://github.com/test/repo" in printed
    assert "Build tool pip packages (1):" in printed
    assert "Pip packages (1):" in printed
    assert "roar reproduce --run abc123def456" in printed


def test_reproduce_run_executes_full_reproduction_and_renders_completion(tmp_path: Path) -> None:
    presenter = MagicMock()
    service = MagicMock()
    service.reproduce.return_value = ReproductionResult(
        success=True,
        repo_dir=tmp_path / "reproduce" / "repo",
        steps_run=2,
        steps_total=2,
        warnings=["Could not pin package"],
    )

    with (
        patch("roar.application.reproduce.service.bootstrap"),
        patch("roar.application.reproduce.service.load_config", return_value={"glaas": {"url": "http://localhost:3001"}}),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch("roar.application.reproduce.service.ReproductionService", return_value=service),
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = True
        mock_glaas_cls.return_value = mock_glaas

        reproduce_artifact(_request(tmp_path, run_pipeline=True, auto_confirm=True), presenter=presenter)

    service.reproduce.assert_called_once()
    call_kwargs = service.reproduce.call_args.kwargs
    assert call_kwargs["run_pipeline"] is True
    assert call_kwargs["auto_confirm"] is True
    printed = "\n".join(call.args[0] for call in presenter.print.call_args_list)
    assert "Reproduction Complete" in printed
    assert "Steps run: 2/2" in printed
    assert "Could not pin package" in printed


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
        patch("roar.application.reproduce.service.load_config", return_value={"glaas": {"url": "http://localhost:3001"}}),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
        patch("roar.application.reproduce.service.ReproductionService", return_value=service),
        patch("roar.application.reproduce.service.PipelineExecutor", return_value=MagicMock()),
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
        patch("roar.application.reproduce.service.load_config", return_value={"glaas": {"url": ""}}),
        patch("roar.application.reproduce.service.GlaasClient") as mock_glaas_cls,
    ):
        mock_glaas = MagicMock()
        mock_glaas.is_configured.return_value = False
        mock_glaas_cls.return_value = mock_glaas

        with pytest.raises(ValueError, match="--out requires a configured GLaaS server"):
            reproduce_artifact(_request(tmp_path, out_path=str(tmp_path / "dag.json")), presenter=presenter)


def test_reproduce_rejects_short_hash_prefix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 8 characters"):
        reproduce_artifact(_request(tmp_path, hash_prefix="short"), presenter=MagicMock())
