"""
Tests for ensemble model combination.

Scenario 6: Train base models, ensemble step consumes all.
"""

import json

import pytest


@pytest.mark.happy_path
class TestEnsemble:
    """Test ensemble model building."""

    def test_ensemble_consumes_base_models(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Train base models, ensemble step consumes all.

        Given: Preprocessed data
        When: Training multiple base models and combining into ensemble
        Then: All runs should complete successfully
        """
        # Preprocess
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        # Train base models with different configurations
        configs = [
            ("--lr=0.01", "--epochs=10"),
            ("--lr=0.001", "--epochs=20"),
            ("--lr=0.01", "--epochs=50"),
        ]

        for i, (lr, epochs) in enumerate(configs, start=1):
            result = roar_cli(
                "run", python_exe, "train.py", "processed.csv", f"base_model_{i}.pkl", lr, epochs
            )
            assert result.returncode == 0
            git_commit(f"After base model {i}")

        # Create ensemble
        result = roar_cli(
            "run",
            python_exe,
            "combine.py",
            "base_model_1.pkl",
            "base_model_2.pkl",
            "base_model_3.pkl",
            "ensemble.json",
        )
        assert result.returncode == 0
        git_commit("After ensemble")

        # Verify DAG exists and tracks key steps
        lineage_result = roar_cli("lineage", "ensemble.json")
        lineage = json.loads(lineage_result.stdout)

        commands = [job["command"] for job in lineage["jobs"]]
        assert any("preprocess.py" in cmd for cmd in commands)
        assert any("train.py" in cmd for cmd in commands)
        assert any("combine.py" in cmd for cmd in commands)

        # Verify all output files exist
        assert (temp_git_repo / "base_model_1.pkl").exists()
        assert (temp_git_repo / "base_model_2.pkl").exists()
        assert (temp_git_repo / "base_model_3.pkl").exists()
        assert (temp_git_repo / "ensemble.json").exists()
