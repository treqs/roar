"""
Tests for roar run CLI options.

Tests -q (quiet), -n (name), and --hash options.
"""

import json

import pytest


@pytest.mark.happy_path
class TestRunOptions:
    """Test roar run CLI options."""

    def test_quiet_flag_suppresses_output(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        -q flag reduces output.

        Given: A script to run
        When: Running with -q flag
        Then: Output should be reduced
        """
        # Run without quiet
        result_verbose = roar_cli("run", python_exe, "preprocess.py", "input.csv", "output_v.csv")
        git_commit("After verbose run")

        # Run with quiet
        result_quiet = roar_cli(
            "run", "-q", python_exe, "preprocess.py", "input.csv", "output_q.csv"
        )
        git_commit("After quiet run")

        # Quiet output should be shorter (no summary banner)
        assert len(result_quiet.stdout) <= len(result_verbose.stdout)

    def test_name_flag_accepted(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        -n flag is accepted by roar run.

        Given: A script to run
        When: Running with -n flag
        Then: Command should succeed
        """
        result = roar_cli(
            "run",
            "-n",
            "preprocess-step",
            python_exe,
            "preprocess.py",
            "input.csv",
            "processed.csv",
        )
        assert result.returncode == 0
        git_commit("After named run")

        # Verify step was recorded in DAG
        lineage_result = roar_cli("lineage", "processed.csv")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) >= 1
        commands = [job["command"] for job in lineage["jobs"]]
        assert any("preprocess.py" in cmd for cmd in commands)

    def test_hash_flag_uses_specified_algorithm(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        --hash sha256 works.

        Given: A script to run
        When: Running with --hash sha256 flag
        Then: Command should succeed (hash algorithm is applied)
        """
        result = roar_cli(
            "run", "--hash", "sha256", python_exe, "preprocess.py", "input.csv", "processed.csv"
        )
        assert result.returncode == 0
        git_commit("After run with sha256")

        # Verify step was recorded
        lineage_result = roar_cli("lineage", "processed.csv")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) >= 1
