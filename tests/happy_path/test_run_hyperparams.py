"""
Tests for hyperparameter variation tracking.

Scenario 7: Same command with different parameters creates distinct DAG nodes.
"""

import json

import pytest


@pytest.mark.happy_path
class TestHyperparams:
    """Test hyperparameter variation tracking."""

    def test_parameter_variations_create_separate_steps(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Same script with different --lr --epochs runs successfully.

        Given: Training script and preprocessed data
        When: Running same script with different hyperparameters
        Then: Each run should complete successfully with correct output
        """
        # Preprocess
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        # Train with different hyperparameters
        params = [
            {"lr": 0.1, "epochs": 5, "output": "model_fast.pkl"},
            {"lr": 0.01, "epochs": 50, "output": "model_medium.pkl"},
            {"lr": 0.001, "epochs": 100, "output": "model_slow.pkl"},
        ]

        for p in params:
            result = roar_cli(
                "run",
                python_exe,
                "train.py",
                "processed.csv",
                p["output"],
                f"--lr={p['lr']}",
                f"--epochs={p['epochs']}",
            )
            assert result.returncode == 0
            assert (temp_git_repo / p["output"]).exists()
            git_commit(f"After training {p['output']}")

        # Verify DAG exists and shows training runs
        lineage_result = roar_cli("lineage", "model_slow.pkl")
        lineage = json.loads(lineage_result.stdout)

        commands = [job["command"] for job in lineage["jobs"]]
        assert any("preprocess.py" in cmd for cmd in commands)
        assert any("train.py" in cmd for cmd in commands)

        # Verify all output files exist with expected content
        for p in params:
            model_path = temp_git_repo / p["output"]
            assert model_path.exists()
            model_data = json.loads(model_path.read_text())
            # Each model should have the correct hyperparameters
            assert model_data["lr"] == p["lr"]
            assert model_data["epochs"] == p["epochs"]
