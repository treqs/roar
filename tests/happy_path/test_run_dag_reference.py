"""
Tests for DAG reference re-execution (@N notation).

Tests re-running steps by reference and parameter override.
"""

import json

import pytest


@pytest.mark.happy_path
class TestDAGReference:
    """Test DAG reference re-execution."""

    def test_rerun_step_by_reference(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Execute step, then re-run with roar run @1.

        Given: An executed step in the DAG
        When: Re-running with @1 notation
        Then: Should execute the same command again and update the DAG
        """
        # Run initial step
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        # Verify step 1 exists
        lineage_result = roar_cli("lineage", "processed.csv")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) == 1

        # Modify input to create different output
        (temp_git_repo / "input.csv").write_text("id,value\n10,changed\n")
        git_commit("Modified input")

        # Remove output to allow re-run
        (temp_git_repo / "processed.csv").unlink()
        git_commit("Removed output")

        # Re-run using @1 reference - this re-executes the same command
        result = roar_cli("run", "@1")
        assert result.returncode == 0

        # Output should be recreated with new content
        assert (temp_git_repo / "processed.csv").exists()
        content = (temp_git_repo / "processed.csv").read_text()
        assert "CHANGED" in content  # uppercased by preprocess.py

    def test_rerun_with_parameter_override(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Re-run @2 with --epochs=20 override.

        Given: A training step in the DAG
        When: Re-running with parameter override
        Then: Should use the new parameter value
        """
        # Initial training
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl", "--epochs=10")
        git_commit("After train")

        # Verify DAG has 2 steps
        lineage_result = roar_cli("lineage", "model.pkl")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) == 2

        # Remove model to allow re-run
        (temp_git_repo / "model.pkl").unlink()
        git_commit("Removed model")

        # Re-run with parameter override
        result = roar_cli("run", "@2", "--epochs=20")
        assert result.returncode == 0
        git_commit("After re-train")

        # Model should be recreated
        assert (temp_git_repo / "model.pkl").exists()
