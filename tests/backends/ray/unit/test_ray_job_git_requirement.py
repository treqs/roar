"""Tests for git requirement behavior when running inside Ray jobs."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from roar.cli.commands.run import run


def _ctx(base_dir: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.is_initialized = True
    ctx.roar_dir = base_dir / ".roar"
    ctx.cwd = base_dir
    return ctx


def _invoke_run(
    base_dir: Path,
    ray_job_id: str | None,
    monkeypatch,
):
    if ray_job_id is None:
        monkeypatch.delenv("RAY_JOB_ID", raising=False)
    else:
        monkeypatch.setenv("RAY_JOB_ID", ray_job_id)

    runner = CliRunner()
    with patch("roar.cli.commands.run.run_command", return_value=0) as mock_run_command:
        result = runner.invoke(run, ["python", "main.py"], obj=_ctx(base_dir))

    return result, mock_run_command


def test_run_in_non_git_dir_without_ray_job_id_now_proceeds(tmp_path, monkeypatch) -> None:
    # roar now runs outside a git repo (lineage captured without a commit),
    # so a non-git dir with no RAY_JOB_ID no longer short-circuits with the
    # old "must be inside a git repository" error — it reaches execution.
    monkeypatch.chdir(tmp_path)
    result, mock_run_command = _invoke_run(tmp_path, None, monkeypatch)

    assert result.exit_code == 0
    assert "roar requires the working directory to be inside a git repository." not in result.output
    mock_run_command.assert_called_once()


@pytest.mark.parametrize("ray_job_id", ["rjob-12345", ""])
def test_run_in_non_git_dir_with_ray_job_id_does_not_raise_git_error(
    tmp_path, monkeypatch, ray_job_id: str
) -> None:
    monkeypatch.chdir(tmp_path)
    result, mock_run_command = _invoke_run(tmp_path, ray_job_id, monkeypatch)

    assert result.exit_code == 0
    assert "roar requires the working directory to be inside a git repository." not in result.output
    mock_run_command.assert_called_once()


def test_run_in_git_dir_works_regardless_of_ray_job_id(tmp_path, monkeypatch) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

    monkeypatch.chdir(repo_dir)

    result_no_ray, mock_run_command_no_ray = _invoke_run(repo_dir, None, monkeypatch)
    assert result_no_ray.exit_code == 0
    mock_run_command_no_ray.assert_called_once()

    result_with_ray, mock_run_command_with_ray = _invoke_run(repo_dir, "rjob-99999", monkeypatch)
    assert result_with_ray.exit_code == 0
    mock_run_command_with_ray.assert_called_once()
