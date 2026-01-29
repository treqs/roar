"""
Tests for multiple model training with selection.

Scenario 3: Train multiple models independently, selection step consumes all.
"""

import json

import pytest


@pytest.mark.happy_path
class TestMultiModel:
    """Test multiple model training and selection."""

    def test_multiple_models_with_selection(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Train 3 models independently, selection step consumes all.

        Given: Preprocessed data
        When: Training 3 models with different parameters, then selecting best
        Then: All runs should complete successfully
        """
        # Step 1: Preprocess
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        # Step 2-4: Train 3 different models
        for i, lr in enumerate([0.01, 0.001, 0.0001], start=1):
            result = roar_cli(
                "run", python_exe, "train.py", "processed.csv", f"model_{i}.pkl", f"--lr={lr}"
            )
            assert result.returncode == 0
            assert (temp_git_repo / f"model_{i}.pkl").exists()
            git_commit(f"After model {i}")

        # Step 5: Select best model (combine reads all models)
        result = roar_cli(
            "run",
            python_exe,
            "combine.py",
            "model_1.pkl",
            "model_2.pkl",
            "model_3.pkl",
            "best_model.json",
        )
        assert result.returncode == 0
        git_commit("After selection")

        # Verify DAG exists and has steps
        lineage_result = roar_cli("lineage", "best_model.json")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) >= 5

        commands = [job["command"] for job in lineage["jobs"]]
        assert any("preprocess.py" in cmd for cmd in commands)
        assert any("train.py" in cmd for cmd in commands)
        assert any("combine.py" in cmd for cmd in commands)

        # Verify all output files exist
        assert (temp_git_repo / "model_1.pkl").exists()
        assert (temp_git_repo / "model_2.pkl").exists()
        assert (temp_git_repo / "model_3.pkl").exists()
        assert (temp_git_repo / "best_model.json").exists()
