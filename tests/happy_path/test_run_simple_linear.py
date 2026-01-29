"""
Tests for simple linear pipeline execution.

Scenario 1: Sequential execution of preprocess -> train -> evaluate
Verifies that provenance is tracked correctly through a linear pipeline.
"""

import json

import pytest


@pytest.mark.happy_path
class TestSimpleLinearPipeline:
    """Test simple linear pipeline execution."""

    def test_sequential_execution_tracks_provenance(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Run preprocess -> train -> evaluate and verify DAG has 3 steps.

        Given: Sample scripts and input data
        When: Running a sequential pipeline with roar run
        Then: The DAG should contain 3 steps in order
        """
        # Step 1: Preprocess
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        assert (temp_git_repo / "processed.csv").exists()
        git_commit("After preprocess")

        # Step 2: Train
        result = roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        assert result.returncode == 0
        assert (temp_git_repo / "model.pkl").exists()
        git_commit("After train")

        # Step 3: Evaluate
        result = roar_cli("run", python_exe, "evaluate.py", "model.pkl", "test.csv", "metrics.json")
        assert result.returncode == 0
        assert (temp_git_repo / "metrics.json").exists()
        git_commit("After evaluate")

        # Verify DAG has 3 steps
        lineage_result = roar_cli("lineage", "metrics.json")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) == 3

        commands = [job["command"] for job in lineage["jobs"]]
        assert any("preprocess.py" in cmd for cmd in commands)
        assert any("train.py" in cmd for cmd in commands)
        assert any("evaluate.py" in cmd for cmd in commands)

    def test_dag_shows_lineage(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify roar show displays job details.

        Given: A completed linear pipeline
        When: Showing details for the final step
        Then: Should show command and job metadata
        """
        # Run the pipeline
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        roar_cli("run", python_exe, "evaluate.py", "model.pkl", "test.csv", "metrics.json")
        git_commit("After evaluate")

        # Show lineage for metrics.json (produced by evaluate step)
        lineage_result = roar_cli("lineage", "metrics.json")
        lineage = json.loads(lineage_result.stdout)

        evaluate_job = next((j for j in lineage["jobs"] if "evaluate.py" in j["command"]), None)
        assert evaluate_job is not None
        assert evaluate_job["exit_code"] is not None
        assert any("metrics.json" in out.get("path", "") for out in evaluate_job["outputs"])
