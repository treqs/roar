"""Tests for skipping git requirement in Ray job execution contexts."""

import importlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

run_module = importlib.import_module("roar.cli.commands.run")
run = run_module.run


def _ctx(base_dir: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.is_initialized = True
    ctx.roar_dir = base_dir / ".roar"
    return ctx


def _invoke_run(base_dir: Path, ray_job_id: str | None, monkeypatch):
    if ray_job_id is None:
        monkeypatch.delenv("RAY_JOB_ID", raising=False)
    else:
        monkeypatch.setenv("RAY_JOB_ID", ray_job_id)

    runner = CliRunner()
    with (
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(run_module, "execute_and_report", return_value=0) as mock_exec,
    ):
        result = runner.invoke(run, ["python", "main.py"], obj=_ctx(base_dir))

    return result, mock_exec


def test_run_non_git_without_ray_job_id_exits_with_git_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RAY_JOB_ID", raising=False)

    runner = CliRunner()
    result = runner.invoke(run, ["python", "main.py"], obj=_ctx(tmp_path))

    assert result.exit_code != 0
    assert "roar requires the working directory to be inside a git repository." in result.output


def test_run_non_git_with_ray_job_id_set_does_not_fail_with_git_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    # Empty string still means the env var is set in the Ray runtime.
    result, mock_exec = _invoke_run(tmp_path, "", monkeypatch)

    assert result.exit_code == 0
    assert "roar requires the working directory to be inside a git repository." not in result.output
    mock_exec.assert_called_once()
    assert mock_exec.call_args.kwargs["repo_root"] == str(tmp_path)


def test_run_git_dir_works_regardless_of_ray_job_id(tmp_path, monkeypatch) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

    monkeypatch.chdir(repo_dir)

    result_no_ray, mock_exec_no_ray = _invoke_run(repo_dir, None, monkeypatch)
    assert result_no_ray.exit_code == 0
    mock_exec_no_ray.assert_called_once()
    assert mock_exec_no_ray.call_args.kwargs["repo_root"] == str(repo_dir)

    result_with_ray, mock_exec_with_ray = _invoke_run(repo_dir, "", monkeypatch)
    assert result_with_ray.exit_code == 0
    mock_exec_with_ray.assert_called_once()
    assert mock_exec_with_ray.call_args.kwargs["repo_root"] == str(repo_dir)
