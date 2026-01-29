"""
Tests for diamond pattern execution.

Scenario 2: Fan-out to branches A/B, merge step consumes both.
"""

import json

import pytest


@pytest.mark.happy_path
class TestDiamondPattern:
    """Test diamond pattern (fan-out/fan-in) execution."""

    def test_diamond_pattern_tracks_multiple_inputs(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Fan-out to branches A/B, merge step consumes both.

        Given: Input data and feature extraction scripts
        When: Running fan-out to two branches, then merging
        Then: The DAG should contain 3 steps
        """
        # Step 1: Extract features A
        result = roar_cli("run", python_exe, "extract_features_a.py", "input.csv", "features_a.csv")
        assert result.returncode == 0
        assert (temp_git_repo / "features_a.csv").exists()
        git_commit("After features A")

        # Step 2: Extract features B
        result = roar_cli("run", python_exe, "extract_features_b.py", "input.csv", "features_b.csv")
        assert result.returncode == 0
        assert (temp_git_repo / "features_b.csv").exists()
        git_commit("After features B")

        # Step 3: Combine (merge)
        result = roar_cli(
            "run", python_exe, "combine.py", "features_a.csv", "features_b.csv", "combined.json"
        )
        assert result.returncode == 0
        assert (temp_git_repo / "combined.json").exists()
        git_commit("After combine")

        # Verify DAG has 3 steps
        lineage_result = roar_cli("lineage", "combined.json")
        lineage = json.loads(lineage_result.stdout)
        assert len(lineage["jobs"]) == 3

        commands = [job["command"] for job in lineage["jobs"]]
        assert any("extract_features_a.py" in cmd for cmd in commands)
        assert any("extract_features_b.py" in cmd for cmd in commands)
        assert any("combine.py" in cmd for cmd in commands)

        combine_job = next((j for j in lineage["jobs"] if "combine.py" in j["command"]), None)
        assert combine_job is not None
        input_paths = [inp.get("path", "") for inp in combine_job["inputs"]]
        assert any("features_a.csv" in p for p in input_paths)
        assert any("features_b.csv" in p for p in input_paths)
