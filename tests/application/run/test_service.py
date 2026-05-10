from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from roar.application.run.requests import BuildRequest, RunRequest
from roar.application.run.service import build_command, run_command


def _run_request(tmp_path: Path, **overrides) -> RunRequest:
    return RunRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        args=overrides.pop("args", ("python", "train.py")),
        quiet=overrides.pop("quiet", None),
        step_name=overrides.pop("step_name", "preprocess"),
        tracer_mode=overrides.pop("tracer_mode", "ptrace"),
        tracer_fallback=overrides.pop("tracer_fallback", False),
        hash_algorithms=overrides.pop("hash_algorithms", ("blake3",)),
        **overrides,
    )


def _build_request(tmp_path: Path, **overrides) -> BuildRequest:
    return BuildRequest(
        roar_dir=overrides.pop("roar_dir", tmp_path / ".roar"),
        cwd=overrides.pop("cwd", tmp_path),
        args=overrides.pop("args", ("make", "-j4")),
        quiet=overrides.pop("quiet", None),
        step_name=overrides.pop("step_name", "bootstrap"),
        tracer_mode=overrides.pop("tracer_mode", "ebpf"),
        tracer_fallback=overrides.pop("tracer_fallback", True),
        hash_algorithms=overrides.pop("hash_algorithms", ("blake3",)),
        **overrides,
    )


def test_run_command_forwards_tracer_flags_and_execution_inputs(tmp_path: Path) -> None:
    planned = SimpleNamespace(
        backend_name="local",
        command=["python", "train.py"],
        execution_role="host",
        finalize_run=None,
    )

    with (
        patch("roar.application.run.service.validate_git_clean", return_value=str(tmp_path)),
        patch("roar.application.run.service.resolve_verbosity", return_value="normal"),
        patch("roar.application.run.service.get_hash_algorithms", return_value=["blake3"]),
        patch("roar.application.run.service.plan_execution_command", return_value=planned),
        patch("roar.application.run.service.execute_and_report", return_value=0) as mock_exec,
    ):
        exit_code = run_command(_run_request(tmp_path))

    assert exit_code == 0
    kwargs = mock_exec.call_args.kwargs
    assert kwargs["roar_dir"] == tmp_path / ".roar"
    assert kwargs["backend_name"] == "local"
    assert kwargs["execution_role"] == "host"
    assert kwargs["command"] == ["python", "train.py"]
    assert kwargs["step_name"] == "preprocess"
    assert kwargs["tracer_mode"] == "ptrace"
    assert kwargs["tracer_fallback"] is False


def test_build_command_forwards_tracer_flags_and_execution_inputs(tmp_path: Path) -> None:
    planned = SimpleNamespace(
        backend_name="local",
        command=["make", "-j4"],
        execution_role="host",
        finalize_run=None,
    )

    with (
        patch("roar.application.run.service.validate_git_clean", return_value=str(tmp_path)),
        patch("roar.application.run.service.resolve_verbosity", return_value="normal"),
        patch("roar.application.run.service.get_hash_algorithms", return_value=["blake3"]),
        patch("roar.application.run.service.plan_execution_command", return_value=planned),
        patch("roar.application.run.service.execute_and_report", return_value=0) as mock_exec,
    ):
        exit_code = build_command(_build_request(tmp_path))

    assert exit_code == 0
    kwargs = mock_exec.call_args.kwargs
    assert kwargs["backend_name"] == "local"
    assert kwargs["execution_role"] == "host"
    assert kwargs["command"] == ["make", "-j4"]
    assert kwargs["job_type"] == "build"
    assert kwargs["step_name"] == "bootstrap"
    assert kwargs["tracer_mode"] == "ebpf"
    assert kwargs["tracer_fallback"] is True


def test_run_command_resolves_dag_reference_and_can_abort_on_stale_upstream(
    tmp_path: Path,
) -> None:
    db_ctx = MagicMock()
    db_ctx.__enter__.return_value = db_ctx
    db_ctx.__exit__.return_value = None
    resolved = SimpleNamespace(
        step_number=2,
        command="python train.py --epochs=20",
        is_build=False,
        stale_upstream=[1],
    )
    presenter = MagicMock()
    report = MagicMock()
    report.show_upstream_stale_warning.return_value = False

    with (
        patch("roar.application.run.service.validate_git_clean", return_value=str(tmp_path)),
        patch("roar.application.run.service.resolve_verbosity", return_value="normal"),
        patch("roar.application.run.service.get_hash_algorithms", return_value=["blake3"]),
        patch("roar.application.run.service.create_database_context", return_value=db_ctx),
        patch("roar.application.run.service.DAGReferenceResolver") as resolver_cls,
        patch("roar.application.run.service.ConsolePresenter", return_value=presenter),
        patch("roar.application.run.service.RunReportPresenter", return_value=report),
        patch("roar.application.run.service.execute_and_report") as mock_exec,
    ):
        resolver_cls.return_value.resolve.return_value = (resolved, None)
        exit_code = run_command(_run_request(tmp_path, args=("@2", "--epochs=20")))

    assert exit_code == 0
    mock_exec.assert_not_called()
    report.show_upstream_stale_warning.assert_called_once_with(2, [1])
    presenter.print.assert_called_with("Aborted.")


def test_run_command_raises_when_backend_does_not_provide_execution_role(tmp_path: Path) -> None:
    planned = SimpleNamespace(
        backend_name="local",
        command=["python", "train.py"],
        execution_role=None,
        finalize_run=None,
    )

    with (
        patch("roar.application.run.service.validate_git_clean", return_value=str(tmp_path)),
        patch("roar.application.run.service.resolve_verbosity", return_value="normal"),
        patch("roar.application.run.service.get_hash_algorithms", return_value=["blake3"]),
        patch("roar.application.run.service.plan_execution_command", return_value=planned),
        pytest.raises(ValueError, match="did not provide an execution role"),
    ):
        run_command(_run_request(tmp_path))
