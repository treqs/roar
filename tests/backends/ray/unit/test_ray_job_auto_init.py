"""Tests for auto-initialization behavior in Ray job execution contexts."""

from unittest.mock import patch

from click.testing import CliRunner

from roar.cli.commands.run import run
from roar.cli.context import RoarContext


def _invoke_run(ctx: RoarContext, monkeypatch, ray_job_id: str | None):
    if ray_job_id is None:
        monkeypatch.delenv("RAY_JOB_ID", raising=False)
    else:
        monkeypatch.setenv("RAY_JOB_ID", ray_job_id)

    runner = CliRunner()
    with patch("roar.cli.commands.run.run_command", return_value=0) as mock_run_command:
        result = runner.invoke(run, ["python", "main.py"], obj=ctx)

    return result, mock_run_command


def test_run_in_uninitialized_tmpdir_without_ray_job_id_exits_with_not_initialized_error(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RAY_JOB_ID", raising=False)
    ctx = RoarContext.create(cwd=tmp_path)

    runner = CliRunner()
    result = runner.invoke(run, ["python", "main.py"], obj=ctx)

    assert result.exit_code == 1
    assert "roar is not initialized in this directory." in result.output
    assert not (tmp_path / ".roar").exists()


def test_run_in_uninitialized_tmpdir_with_ray_job_id_auto_inits_and_does_not_fail_with_init_error(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = RoarContext.create(cwd=tmp_path)

    result, mock_run_command = _invoke_run(ctx, monkeypatch, "rjob-12345")

    assert result.exit_code == 0
    assert "roar is not initialized in this directory." not in result.output
    assert "Created " not in result.output
    assert (tmp_path / ".roar").is_dir()
    assert (tmp_path / ".roar" / "roar.db").is_file()
    assert (tmp_path / ".roar" / "config.toml").is_file()
    mock_run_command.assert_called_once()


def test_run_in_initialized_dir_works_regardless_of_ray_job_id(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".roar").mkdir()
    ctx = RoarContext.create(cwd=tmp_path)

    result_no_ray, mock_run_command_no_ray = _invoke_run(ctx, monkeypatch, None)
    assert result_no_ray.exit_code == 0
    assert "roar is not initialized in this directory." not in result_no_ray.output
    mock_run_command_no_ray.assert_called_once()

    result_with_ray, mock_run_command_with_ray = _invoke_run(ctx, monkeypatch, "rjob-99999")
    assert result_with_ray.exit_code == 0
    assert "roar is not initialized in this directory." not in result_with_ray.output
    mock_run_command_with_ray.assert_called_once()
