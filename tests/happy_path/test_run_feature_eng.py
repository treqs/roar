"""
Tests for multiple feature extractors merging.

Scenario 4: Multiple extraction scripts merge into single training input.
"""

import json

import pytest


@pytest.mark.happy_path
class TestFeatureEngineering:
    """Test multiple feature extractors merging."""

    def test_feature_merge_tracks_all_inputs(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Multiple extraction scripts merge into single training input.

        Given: Input data and multiple feature extraction scripts
        When: Extracting multiple feature sets and merging
        Then: DAG should track all 4 steps
        """
        # Extract 3 different feature sets
        for name in ["features_a", "features_b", "features_c"]:
            result = roar_cli("run", python_exe, f"extract_{name}.py", "input.csv", f"{name}.csv")
            assert result.returncode == 0
            git_commit(f"After {name}")

        # Merge all features
        result = roar_cli(
            "run",
            python_exe,
            "combine.py",
            "features_a.csv",
            "features_b.csv",
            "features_c.csv",
            "merged_features.json",
        )
        assert result.returncode == 0
        git_commit("After merge")

        # Verify DAG has 4 steps
        lineage_result = roar_cli("lineage", "merged_features.json")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) == 4

        commands = [job["command"] for job in lineage["jobs"]]
        assert any("combine.py" in cmd for cmd in commands)
