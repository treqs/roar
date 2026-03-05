"""Regression test for RAY_JOB_ID-based bypass in `roar run`."""

import importlib
from unittest.mock import patch

from click.testing import CliRunner

from roar.cli.commands.run import run
from roar.cli.context import RoarContext

run_module = importlib.import_module("roar.cli.commands.run")


def test_run_with_ray_job_id_auto_inits_and_bypasses_git_check(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAY_JOB_ID", "rjob-abc123")
    monkeypatch.delenv("RAY_JOB_CONFIG_JSON_ENV_VAR", raising=False)
    ctx = RoarContext.create(cwd=tmp_path)

    runner = CliRunner()
    with (
        patch.object(run_module, "get_quiet_setting", return_value=False),
        patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
        patch.object(run_module, "execute_and_report", return_value=0) as mock_exec,
    ):
        result = runner.invoke(run, ["python", "main.py"], obj=ctx)

    assert result.exit_code == 0, (
        "Expected `roar run` inside a Ray job (RAY_JOB_ID set) to auto-init in a non-git "
        f"directory and continue, but it failed with output:\n{result.output}"
    )
    assert (tmp_path / ".roar").is_dir(), (
        "Expected auto-init to create .roar when RAY_JOB_ID is present."
    )
    assert "roar requires the working directory to be inside a git repository." not in result.output, (
        "Expected git validation to be bypassed when RAY_JOB_ID is present."
    )
    mock_exec.assert_called_once()

