"""Integration tests for ray job submit interception in `roar run`."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.run import run

run_module = importlib.import_module("roar.cli.commands.run")


def _ctx() -> MagicMock:
    obj = MagicMock()
    obj.is_initialized = True
    obj.roar_dir = Path("/tmp/repo/.roar")
    return obj


def test_run_with_ray_job_submit_calls_rewrite() -> None:
    runner = CliRunner()
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
        patch.object(run_module, "validate_git_clean", return_value="/tmp/repo"),
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(
            run_module,
            "plan_execution_command",
            return_value=SimpleNamespace(
                backend_name="ray",
                command=rewritten_command,
                execution_role="submit",
                session_id=None,
                finalize_run=None,
            ),
        ) as mock_rewrite,
        patch.object(run_module, "execute_and_report", return_value=0) as mock_exec,
    ):
        result = runner.invoke(run, original_command, obj=_ctx())

    assert result.exit_code == 0
    mock_rewrite.assert_called_once_with(original_command)
    assert mock_exec.call_args.kwargs["backend_name"] == "ray"
    assert mock_exec.call_args.kwargs["execution_role"] == "submit"
    assert mock_exec.call_args.kwargs["command"] == rewritten_command


def test_run_with_non_ray_command_does_not_call_rewrite() -> None:
    runner = CliRunner()

    with (
        patch.object(run_module, "validate_git_clean", return_value="/tmp/repo"),
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(
            run_module,
            "plan_execution_command",
            return_value=SimpleNamespace(
                backend_name="local",
                command=["python", "main.py"],
                execution_role="host",
                session_id=None,
                finalize_run=None,
            ),
        ) as mock_rewrite,
        patch.object(run_module, "execute_and_report", return_value=0) as mock_exec,
    ):
        result = runner.invoke(run, ["python", "main.py"], obj=_ctx())

    assert result.exit_code == 0
    mock_rewrite.assert_called_once_with(["python", "main.py"])
    assert mock_exec.call_args.kwargs["backend_name"] == "local"
    assert mock_exec.call_args.kwargs["execution_role"] == "host"
    assert mock_exec.call_args.kwargs["command"] == ["python", "main.py"]


def test_run_with_ray_job_submit_triggers_post_run_finalizer() -> None:
    runner = CliRunner()
    ctx = _ctx()
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
        patch.object(run_module, "validate_git_clean", return_value="/tmp/repo"),
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(
            run_module,
            "plan_execution_command",
            return_value=SimpleNamespace(
                backend_name="ray",
                command=rewritten_command,
                execution_role="submit",
                session_id="session-123",
                finalize_run=finalizer,
            ),
        ),
        patch.object(run_module, "execute_and_report", return_value=0),
    ):
        result = runner.invoke(run, original_command, obj=ctx)

    assert result.exit_code == 0
    finalizer.assert_called_once_with(ctx)
