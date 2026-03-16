"""Happy-path coverage for local query commands."""

from __future__ import annotations

import pytest


@pytest.mark.happy_path
class TestQueryCommands:
    """Product-path checks for status, log, and show."""

    def test_status_log_and_show_report_active_session_state(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ) -> None:
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        result = roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        assert result.returncode == 0
        git_commit("After train")

        status_result = roar_cli("status")
        assert status_result.returncode == 0
        assert "Build steps: 0" in status_result.stdout
        assert "Run steps:   2" in status_result.stdout
        assert "Tracked artifacts" in status_result.stdout

        log_result = roar_cli("log")
        assert log_result.returncode == 0
        assert "Job Log (2 jobs)" in log_result.stdout
        assert "@1" in log_result.stdout
        assert "@2" in log_result.stdout
        assert "preprocess.py" in log_result.stdout
        assert "train.py" in log_result.stdout

        show_result = roar_cli("show", "@2")
        assert show_result.returncode == 0
        assert "Job:" in show_result.stdout
        assert "train.py" in show_result.stdout
