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
        "roar",
        "run",
        "python",
        "main.py",
    ]

    with (
        patch.object(run_module, "validate_git_clean", return_value="/tmp/repo"),
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(
            run_module,
            "maybe_rewrite_ray_job_submit",
            return_value=SimpleNamespace(command=rewritten_command, session_id=None),
        ) as mock_rewrite,
        patch.object(run_module, "execute_and_report", return_value=0) as mock_exec,
    ):
        result = runner.invoke(run, original_command, obj=_ctx())

    assert result.exit_code == 0
    mock_rewrite.assert_called_once_with(original_command)
    assert mock_exec.call_args.kwargs["command"] == rewritten_command


def test_run_with_non_ray_command_does_not_call_rewrite() -> None:
    runner = CliRunner()

    with (
        patch.object(run_module, "validate_git_clean", return_value="/tmp/repo"),
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(run_module, "maybe_rewrite_ray_job_submit") as mock_rewrite,
        patch.object(run_module, "execute_and_report", return_value=0) as mock_exec,
    ):
        result = runner.invoke(run, ["python", "main.py"], obj=_ctx())

    assert result.exit_code == 0
    mock_rewrite.assert_not_called()
    assert mock_exec.call_args.kwargs["command"] == ["python", "main.py"]


def test_run_with_ray_job_submit_triggers_auto_reconstitution() -> None:
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
        "roar",
        "run",
        "python",
        "main.py",
    ]

    fake_result = SimpleNamespace(jobs_merged=2, artifacts_merged=3, fragments_processed=4)

    with (
        patch.object(run_module, "validate_git_clean", return_value="/tmp/repo"),
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(
            run_module,
            "maybe_rewrite_ray_job_submit",
            return_value=SimpleNamespace(command=rewritten_command, session_id="session-123"),
        ),
        patch.object(run_module, "execute_and_report", return_value=0),
        patch.object(run_module, "get_glaas_url", return_value="http://localhost:3001"),
        patch.object(run_module, "load_key", return_value={"token": "ab" * 32}),
        patch.object(run_module, "FragmentReconstituter") as mock_reconstituter_cls,
    ):
        mock_reconstituter_cls.return_value.reconstitute.return_value = fake_result
        result = runner.invoke(run, original_command, obj=_ctx())

    assert result.exit_code == 0
    mock_reconstituter_cls.assert_called_once_with(
        session_id="session-123",
        token="ab" * 32,
        glaas_url="http://localhost:3001",
        roar_db_path=Path("/tmp/repo/.roar/roar.db"),
    )
    mock_reconstituter_cls.return_value.reconstitute.assert_called_once_with()
    assert "[roar] lineage reconstituted: 2 jobs, 3 artifacts" in result.output
