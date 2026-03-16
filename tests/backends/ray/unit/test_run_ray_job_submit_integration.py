"""Integration tests for ray job submit interception in the run application service."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from roar.application.run.requests import RunRequest
from roar.application.run.service import run_command


def _request(*args: str) -> RunRequest:
    return RunRequest(
        roar_dir=Path("/tmp/repo/.roar"),
        cwd=Path("/tmp/repo"),
        args=tuple(args),
    )


def test_run_with_ray_job_submit_calls_rewrite() -> None:
    original_command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
        "--",
        "python",
        "main.py",
    ]
    rewritten_command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
        "--runtime-env-json",
        '{"pip":["roar-cli==1.2.3"]}',
        "--",
        "python",
        "main.py",
    ]

    with (
        patch("roar.application.run.service.validate_git_clean", return_value="/tmp/repo"),
        patch("roar.application.run.service.get_quiet_setting", return_value=False),
        patch("roar.application.run.service.get_hash_algorithms", return_value=["blake3"]),
        patch(
            "roar.application.run.service.plan_execution_command",
            return_value=SimpleNamespace(
                backend_name="ray",
                command=rewritten_command,
                execution_role="submit",
                session_id=None,
                finalize_run=None,
            ),
        ) as mock_rewrite,
        patch("roar.application.run.service.execute_and_report", return_value=0) as mock_exec,
    ):
        result = run_command(_request(*original_command))

    assert result == 0
    mock_rewrite.assert_called_once_with(original_command)
    assert mock_exec.call_args.kwargs["backend_name"] == "ray"
    assert mock_exec.call_args.kwargs["execution_role"] == "submit"
    assert mock_exec.call_args.kwargs["command"] == rewritten_command


def test_run_with_non_ray_command_does_not_call_rewrite() -> None:
    with (
        patch("roar.application.run.service.validate_git_clean", return_value="/tmp/repo"),
        patch("roar.application.run.service.get_quiet_setting", return_value=False),
        patch("roar.application.run.service.get_hash_algorithms", return_value=["blake3"]),
        patch(
            "roar.application.run.service.plan_execution_command",
            return_value=SimpleNamespace(
                backend_name="local",
                command=["python", "main.py"],
                execution_role="host",
                session_id=None,
                finalize_run=None,
            ),
        ) as mock_rewrite,
        patch("roar.application.run.service.execute_and_report", return_value=0) as mock_exec,
    ):
        result = run_command(_request("python", "main.py"))

    assert result == 0
    mock_rewrite.assert_called_once_with(["python", "main.py"])
    assert mock_exec.call_args.kwargs["backend_name"] == "local"
    assert mock_exec.call_args.kwargs["execution_role"] == "host"
    assert mock_exec.call_args.kwargs["command"] == ["python", "main.py"]


def test_run_with_ray_job_submit_triggers_post_run_finalizer() -> None:
    original_command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
        "--",
        "python",
        "main.py",
    ]
    rewritten_command = [
        "ray",
        "job",
        "submit",
        "--address",
        "http://localhost:8265",
        "--working-dir",
        ".",
        "--runtime-env-json",
        '{"pip":["roar-cli==1.2.3"]}',
        "--",
        "python",
        "main.py",
    ]

    finalizer = MagicMock()

    with (
        patch("roar.application.run.service.validate_git_clean", return_value="/tmp/repo"),
        patch("roar.application.run.service.get_quiet_setting", return_value=False),
        patch("roar.application.run.service.get_hash_algorithms", return_value=["blake3"]),
        patch(
            "roar.application.run.service.plan_execution_command",
            return_value=SimpleNamespace(
                backend_name="ray",
                command=rewritten_command,
                execution_role="submit",
                session_id="session-123",
                finalize_run=finalizer,
            ),
        ),
        patch("roar.application.run.service.execute_and_report", return_value=0),
    ):
        result = run_command(_request(*original_command))

    assert result == 0
    finalizer.assert_called_once()
    assert finalizer.call_args.args[0].roar_dir == Path("/tmp/repo/.roar")
