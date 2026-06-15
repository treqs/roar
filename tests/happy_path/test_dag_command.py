"""
Integration tests for the roar dag command.

Tests the dag command with various pipeline configurations.
"""

import json

import pytest


def _run_preprocess(roar_cli, git_commit, python_exe) -> None:
    result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
    assert result.returncode == 0
    git_commit("After preprocess")


def _run_train(roar_cli, git_commit, python_exe) -> None:
    result = roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
    assert result.returncode == 0
    git_commit("After train")


def _run_evaluate(roar_cli, git_commit, python_exe) -> None:
    result = roar_cli("run", python_exe, "evaluate.py", "model.pkl", "test.csv", "metrics.json")
    assert result.returncode == 0
    git_commit("After evaluate")


def _rerun_preprocess_with_modified_input(
    temp_git_repo,
    roar_cli,
    git_commit,
    python_exe,
    updated_input: str,
) -> None:
    (temp_git_repo / "input.csv").write_text(updated_input)
    git_commit("Modified input")

    result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
    assert result.returncode == 0
    git_commit("Rerun preprocess")


def _write_script(temp_git_repo, git_commit, name: str, body: str, commit_message: str) -> None:
    (temp_git_repo / name).write_text(body)
    git_commit(commit_message)


def _run_roar_and_commit(roar_cli, git_commit, commit_message: str, *args: str) -> None:
    result = roar_cli(*args)
    assert result.returncode == 0
    git_commit(commit_message)


@pytest.mark.happy_path
class TestDagCommand:
    """Test roar dag command functionality."""

    def test_dag_json_output(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify --json flag produces valid JSON output.

        Given: A pipeline with steps
        When: Running roar dag --json
        Then: Output should be valid JSON with expected structure
        """
        # Run a simple step
        _run_preprocess(roar_cli, git_commit, python_exe)

        # Run dag with JSON output
        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        # Parse JSON output
        dag_data = json.loads(result.stdout)

        assert "nodes" in dag_data
        assert "artifacts" in dag_data
        assert "total_steps" in dag_data
        assert "stale_count" in dag_data
        assert "session_id" in dag_data

        assert dag_data["total_steps"] == 1
        assert len(dag_data["nodes"]) == 1

        node = dag_data["nodes"][0]
        assert node["step_number"] == 1
        assert "preprocess.py" in node["command"]
        assert node["state"] == "active"
        assert "metrics" in node

    def test_dag_shows_stale_steps(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify stale steps are marked in dag output.

        Given: A pipeline where preprocess is rerun
        When: Running roar dag
        Then: The train step should be marked as stale
        """
        # Initial pipeline
        _run_preprocess(roar_cli, git_commit, python_exe)
        _run_train(roar_cli, git_commit, python_exe)
        _rerun_preprocess_with_modified_input(
            temp_git_repo,
            roar_cli,
            git_commit,
            python_exe,
            "id,value\n1,modified\n2,data\n",
        )

        # Check dag - train should be stale
        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["stale_count"] >= 1

        # Find the train step
        train_step = next((n for n in dag_data["nodes"] if "train.py" in n["command"]), None)
        assert train_step is not None
        assert train_step["state"] == "stale"

    def test_dag_expanded_view(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify --expanded flag shows all executions.

        Given: A step that has been run multiple times
        When: Running roar dag --expanded --json
        Then: All executions should be visible
        """
        # Initial run
        _run_preprocess(roar_cli, git_commit, python_exe)

        # Modify and rerun
        _rerun_preprocess_with_modified_input(
            temp_git_repo,
            roar_cli,
            git_commit,
            python_exe,
            "id,value\n1,modified\n",
        )

        # Check expanded view
        result = roar_cli("dag", "--expanded", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["is_expanded"] is True

        # Should have more nodes in expanded view (both executions)
        preprocess_nodes = [n for n in dag_data["nodes"] if "preprocess.py" in n["command"]]
        assert len(preprocess_nodes) == 2

    def test_dag_empty_session(
        self,
        temp_git_repo,
        roar_cli,
    ):
        """
        Verify dag handles empty session gracefully.

        Given: An initialized roar with no steps
        When: Running roar dag
        Then: Should show appropriate message about no active session
        """
        # When no commands have been run, there's no active session
        result = roar_cli("dag", "--no-color", check=False)
        # Either no session message or empty pipeline is acceptable
        # Error messages may be in stdout or stderr depending on error handling
        combined = result.stdout + result.stderr
        assert (
            "No active session" in combined
            or "No steps in pipeline" in combined
            or "Pipeline: 0 steps" in combined
        )

    # =========================================================================
    # Scenario Coverage Tests (from SCENARIO.md)
    # =========================================================================

    def test_dag_feature_engineering_fan_in(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Test feature engineering fan-in pattern (Scenario 4).

        Given: Multiple feature extraction jobs merging into train
        When: Running roar dag --json
        Then: Merge step should show correct consumed count
        """
        # Use existing extract_features_* scripts from sample_scripts
        # Run feature extraction (fan-out from input)
        _run_roar_and_commit(
            roar_cli,
            git_commit,
            "After features_a",
            "run",
            python_exe,
            "extract_features_a.py",
            "input.csv",
            "features_a.csv",
        )
        _run_roar_and_commit(
            roar_cli,
            git_commit,
            "After features_b",
            "run",
            python_exe,
            "extract_features_b.py",
            "input.csv",
            "features_b.csv",
        )
        _run_roar_and_commit(
            roar_cli,
            git_commit,
            "After features_c",
            "run",
            python_exe,
            "extract_features_c.py",
            "input.csv",
            "features_c.csv",
        )

        # Combine features (fan-in)
        _run_roar_and_commit(
            roar_cli,
            git_commit,
            "After combine",
            "run",
            python_exe,
            "combine.py",
            "features_a.csv",
            "features_b.csv",
            "features_c.csv",
            "combined.json",
        )

        # Train on combined features
        _run_roar_and_commit(
            roar_cli,
            git_commit,
            "After train",
            "run",
            python_exe,
            "train.py",
            "combined.json",
            "model.pkl",
        )

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["total_steps"] == 5

        # Find combine step
        combine_step = next((n for n in dag_data["nodes"] if "combine.py" in n["command"]), None)
        assert combine_step is not None
        assert combine_step["metrics"]["consumed"] == 3
        assert len(combine_step["dependencies"]) == 3

    def test_dag_build_steps(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Test build steps (Scenario 8).

        Given: Build step followed by run steps
        When: Running roar dag --no-color
        Then: @B prefix should appear for build steps
        """
        # Create a simple build script
        _write_script(
            temp_git_repo,
            git_commit,
            "setup_env.py",
            """
import json

# Simulate setting up environment
config = {"version": "1.0", "setup_complete": True}
with open("config.json", "w") as f:
    json.dump(config, f)

print("Environment setup complete")
""",
            "Add build script",
        )

        # Run build step
        _run_roar_and_commit(
            roar_cli, git_commit, "After build", "build", python_exe, "setup_env.py"
        )

        # Run a regular step that uses the config
        _write_script(
            temp_git_repo,
            git_commit,
            "use_config.py",
            """
import json

with open("config.json", "r") as f:
    config = json.load(f)

with open("output.json", "w") as f:
    json.dump({"used_config": config}, f)

print("Used config")
""",
            "Add use_config script",
        )

        _run_roar_and_commit(
            roar_cli, git_commit, "After use_config", "run", python_exe, "use_config.py"
        )

        result = roar_cli("dag", "--no-color")
        assert result.returncode == 0

        output = result.stdout
        assert "@B1" in output  # Build step has @B prefix
        assert "@2" in output  # Regular step has @ prefix
        assert "B = build step" in output  # Legend shows build step indicator

    def test_dag_cascade_invalidation(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Test cascade invalidation (Scenario 9).

        Given: A linear pipeline where root job is rerun
        When: Running roar dag --json
        Then: Downstream steps should be marked stale with correct stale_count
        """
        # Build pipeline: preprocess -> train -> evaluate
        _run_preprocess(roar_cli, git_commit, python_exe)
        _run_train(roar_cli, git_commit, python_exe)
        _run_evaluate(roar_cli, git_commit, python_exe)
        _rerun_preprocess_with_modified_input(
            temp_git_repo,
            roar_cli,
            git_commit,
            python_exe,
            "id,value\n1,new_data\n2,changed\n",
        )

        # Check cascade invalidation
        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        # Both train and evaluate should be stale
        assert dag_data["stale_count"] >= 2

        train_step = next((n for n in dag_data["nodes"] if "train.py" in n["command"]), None)
        evaluate_step = next((n for n in dag_data["nodes"] if "evaluate.py" in n["command"]), None)

        assert train_step is not None and train_step["state"] == "stale"
        assert evaluate_step is not None and evaluate_step["state"] == "stale"

    def test_dag_partial_branch_invalidation(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Test partial branch invalidation (Scenario 10).

        Given: Diamond pattern with one branch rerun
        When: Running roar dag --json
        Then: Only affected branch should be marked stale
        """
        # Create split script
        _write_script(
            temp_git_repo,
            git_commit,
            "split.py",
            """
import sys

with open("input.csv", "r") as f:
    data = f.read()

# Split into two parts
with open("part_a.csv", "w") as f:
    f.write(data[:len(data)//2])

with open("part_b.csv", "w") as f:
    f.write(data[len(data)//2:])

print("Split input into part_a and part_b")
""",
            "Add split script",
        )

        # Build diamond: split -> (train_a, train_b) -> merge
        _run_roar_and_commit(roar_cli, git_commit, "After split", "run", python_exe, "split.py")

        # Create train_a script
        _write_script(
            temp_git_repo,
            git_commit,
            "train_a.py",
            """
import json
with open("part_a.csv", "r") as f:
    data = f.read()
with open("model_a.pkl", "w") as f:
    json.dump({"model": "a", "hash": hash(data)}, f)
print("Trained model_a")
""",
            "Add train_a script",
        )

        _run_roar_and_commit(roar_cli, git_commit, "After train_a", "run", python_exe, "train_a.py")

        # Create train_b script
        _write_script(
            temp_git_repo,
            git_commit,
            "train_b.py",
            """
import json
with open("part_b.csv", "r") as f:
    data = f.read()
with open("model_b.pkl", "w") as f:
    json.dump({"model": "b", "hash": hash(data)}, f)
print("Trained model_b")
""",
            "Add train_b script",
        )

        _run_roar_and_commit(roar_cli, git_commit, "After train_b", "run", python_exe, "train_b.py")

        # Create merge script
        _write_script(
            temp_git_repo,
            git_commit,
            "merge_models.py",
            """
import json
with open("model_a.pkl", "r") as f:
    model_a = json.load(f)
with open("model_b.pkl", "r") as f:
    model_b = json.load(f)
with open("final_model.pkl", "w") as f:
    json.dump({"a": model_a, "b": model_b}, f)
print("Merged models")
""",
            "Add merge script",
        )

        _run_roar_and_commit(
            roar_cli, git_commit, "After merge", "run", python_exe, "merge_models.py"
        )

        # Rerun only train_a (branch A)
        (temp_git_repo / "part_a.csv").write_text("modified,data\n")
        git_commit("Modified part_a")

        _run_roar_and_commit(roar_cli, git_commit, "Rerun train_a", "run", python_exe, "train_a.py")

        # Check partial invalidation
        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        # Merge should be stale (consumes from both branches)
        merge_step = next((n for n in dag_data["nodes"] if "merge_models.py" in n["command"]), None)
        assert merge_step is not None
        assert merge_step["state"] == "stale"

    # =========================================================================
    # Edge Case Tests
    # =========================================================================

    @pytest.mark.large_pipeline
    def test_dag_large_pipeline(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_data,
        python_exe,
    ):
        """
        Test large pipeline with 10+ steps.

        Given: A pipeline with many steps
        When: Running roar dag --json
        Then: Rendering should not break and all steps visible
        """
        # Create a chain of 12 steps
        for i in range(1, 13):
            script = temp_git_repo / f"step_{i}.py"
            input_file = "input.csv" if i == 1 else f"output_{i - 1}.txt"
            output_file = f"output_{i}.txt"
            script.write_text(f'''
with open("{input_file}", "r") as f:
    data = f.read()

with open("{output_file}", "w") as f:
    f.write(f"Step {i}: {{data[:50]}}")

print(f"Completed step {i}")
''')
        git_commit("Add 12 step scripts")

        # Run all steps
        for i in range(1, 13):
            _run_roar_and_commit(
                roar_cli, git_commit, f"After step_{i}", "run", python_exe, f"step_{i}.py"
            )

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["total_steps"] == 12

        # Text output should also work
        result = roar_cli("dag", "--no-color")
        assert result.returncode == 0
        assert "Pipeline: 12 steps" in result.stdout

    def test_dag_deep_nesting(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_data,
        python_exe,
    ):
        """
        Test deeply nested linear pipeline (5+ levels).

        Given: A deeply nested linear pipeline
        When: Running roar dag --no-color
        Then: Tree indentation should be correct
        """
        # Create 6 linear steps
        for i in range(1, 7):
            script = temp_git_repo / f"level_{i}.py"
            input_file = "input.csv" if i == 1 else f"level_{i - 1}_output.txt"
            output_file = f"level_{i}_output.txt"
            script.write_text(f'''
with open("{input_file}", "r") as f:
    data = f.read()

with open("{output_file}", "w") as f:
    f.write(f"Level {i}: {{data[:20]}}")

print(f"Completed level {i}")
''')
        git_commit("Add 6 level scripts")

        # Run all levels
        for i in range(1, 7):
            _run_roar_and_commit(
                roar_cli, git_commit, f"After level_{i}", "run", python_exe, f"level_{i}.py"
            )

        result = roar_cli("dag", "--no-color")
        assert result.returncode == 0

        output = result.stdout
        assert "Pipeline: 6 steps" in output
        assert "@1" in output
        assert "@6" in output

        # Verify JSON structure is correct
        result = roar_cli("dag", "--json")
        dag_data = json.loads(result.stdout)

        # Each step (except first) should have exactly one dependency
        for node in dag_data["nodes"]:
            if node["step_number"] > 1:
                assert len(node["dependencies"]) == 1
                assert node["dependencies"][0] == node["step_number"] - 1

    def test_dag_named_steps(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Test that --name creates canonical named nodes in DAG output.

        Given: Steps run with explicit names
        When: Running roar dag --json
        Then: JSON output should surface the label-backed step name
        """
        _run_roar_and_commit(
            roar_cli,
            git_commit,
            "After preprocess",
            "run",
            "--name",
            "preprocess",
            python_exe,
            "preprocess.py",
            "input.csv",
            "processed.csv",
        )
        _run_roar_and_commit(
            roar_cli,
            git_commit,
            "After train",
            "run",
            "--name",
            "train",
            python_exe,
            "train.py",
            "processed.csv",
            "model.pkl",
        )

        result = roar_cli("dag", "--json")
        assert result.returncode == 0
        dag_data = json.loads(result.stdout)

        preprocess_step = next(
            (n for n in dag_data["nodes"] if "preprocess.py" in n["command"]), None
        )
        train_step = next((n for n in dag_data["nodes"] if "train.py" in n["command"]), None)

        assert preprocess_step is not None
        assert train_step is not None

        assert preprocess_step["step_name"] == "preprocess"
        assert preprocess_step["labels"] == {"name": "preprocess"}
        assert train_step["step_name"] == "train"
        assert train_step["labels"] == {"name": "train"}

    def test_dag_multiple_artifacts(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_data,
        python_exe,
    ):
        """
        Test pipeline producing multiple output files.

        Given: A pipeline that produces multiple artifacts
        When: Running roar dag --json
        Then: All artifacts should be listed
        """
        # Create script that produces multiple outputs
        _write_script(
            temp_git_repo,
            git_commit,
            "multi_output.py",
            """
import json

with open("input.csv", "r") as f:
    data = f.read()

# Produce multiple outputs
with open("output_1.json", "w") as f:
    json.dump({"part": 1, "data": data[:10]}, f)

with open("output_2.json", "w") as f:
    json.dump({"part": 2, "data": data[10:20]}, f)

with open("output_3.json", "w") as f:
    json.dump({"part": 3, "data": data[20:]}, f)

print("Produced 3 outputs")
""",
            "Add multi_output script",
        )

        _run_roar_and_commit(
            roar_cli, git_commit, "After multi_output", "run", python_exe, "multi_output.py"
        )

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        # Step should show 3 outputs in metrics
        node = dag_data["nodes"][0]
        assert node["metrics"]["outputs"] == 3

        # Artifacts list should include multiple files
        artifact_paths = [a["path"] for a in dag_data["artifacts"]]
        assert any("output_1.json" in p for p in artifact_paths)
        assert any("output_2.json" in p for p in artifact_paths)
        assert any("output_3.json" in p for p in artifact_paths)

    # =========================================================================
    # Artifact State Tests
    # =========================================================================

    def test_dag_stale_artifact_state(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify stale artifacts are marked with stale state.

        Given: A pipeline where preprocess is rerun, making train's output stale
        When: Running roar dag --json
        Then: Artifacts from stale steps should have state="stale"
        """
        # Build pipeline
        _run_preprocess(roar_cli, git_commit, python_exe)
        _run_train(roar_cli, git_commit, python_exe)
        _rerun_preprocess_with_modified_input(
            temp_git_repo,
            roar_cli,
            git_commit,
            python_exe,
            "id,value\n1,modified\n2,data\n",
        )

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        # Find train step's artifact (model.pkl)
        train_artifacts = [a for a in dag_data["artifacts"] if "model.pkl" in a["path"]]
        assert len(train_artifacts) > 0
        assert train_artifacts[0]["state"] == "stale"
        assert dag_data["stale_artifact_count"] >= 1

    def test_dag_show_artifacts_option(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify --show-artifacts displays intermediate artifacts.

        Given: A multi-step pipeline
        When: Running roar dag --show-artifacts --json
        Then: Intermediate artifacts should be included
        """
        _run_preprocess(roar_cli, git_commit, python_exe)
        _run_train(roar_cli, git_commit, python_exe)

        # Without --show-artifacts, only terminal artifacts shown
        result = roar_cli("dag", "--json")
        assert result.returncode == 0
        dag_data_default = json.loads(result.stdout)

        # With --show-artifacts, intermediate artifacts included
        result = roar_cli("dag", "--show-artifacts", "--json")
        assert result.returncode == 0
        dag_data_all = json.loads(result.stdout)

        # Should have more artifacts with --show-artifacts
        assert len(dag_data_all["artifacts"]) >= len(dag_data_default["artifacts"])

        # Check intermediate artifact has is_terminal=False
        intermediate = [a for a in dag_data_all["artifacts"] if not a["is_terminal"]]
        if intermediate:
            assert intermediate[0]["is_terminal"] is False

    def test_dag_stale_only_option(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify --stale-only filters to only stale steps and artifacts.

        Given: A pipeline with some stale steps
        When: Running roar dag --stale-only --json
        Then: Only stale steps and artifacts should be shown
        """
        # Build pipeline
        _run_preprocess(roar_cli, git_commit, python_exe)
        _run_train(roar_cli, git_commit, python_exe)
        _rerun_preprocess_with_modified_input(
            temp_git_repo,
            roar_cli,
            git_commit,
            python_exe,
            "id,value\n1,new\n",
        )

        # Without --stale-only
        result = roar_cli("dag", "--json")
        dag_data_all = json.loads(result.stdout)
        all_steps_count = len(dag_data_all["nodes"])

        # With --stale-only
        result = roar_cli("dag", "--stale-only", "--json")
        assert result.returncode == 0
        dag_data_stale = json.loads(result.stdout)

        # Should have fewer steps
        assert len(dag_data_stale["nodes"]) < all_steps_count

        # All shown steps should be stale
        for node in dag_data_stale["nodes"]:
            assert node["state"] == "stale"

    def test_dag_artifact_consumer_tracking(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify artifacts track their consumer steps.

        Given: A pipeline where processed.csv is consumed by train
        When: Running roar dag --show-artifacts --json
        Then: processed.csv artifact should have train step in consumer_steps
        """
        _run_preprocess(roar_cli, git_commit, python_exe)
        _run_train(roar_cli, git_commit, python_exe)

        result = roar_cli("dag", "--show-artifacts", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        # Find processed.csv artifact
        processed_artifacts = [a for a in dag_data["artifacts"] if "processed.csv" in a["path"]]

        if processed_artifacts:
            processed = processed_artifacts[0]
            # Train step should be in consumer_steps
            assert len(processed["consumer_steps"]) > 0
            # processed.csv is not terminal (consumed by train)
            assert processed["is_terminal"] is False

    def test_dag_superseded_propagates_downstream(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify superseded state propagates to downstream steps and artifacts.

        Given: A linear pipeline where step1 is re-run (creating superseded version)
        When: Running roar dag --expanded --json
        Then: All downstream artifacts from superseded step1 should be marked superseded

        Example:
            Step 1 (superseded) -> artifact_a -> Step 2 (active) -> artifact_b
            Expected: artifact_a = superseded, artifact_b = superseded
        """
        # Build initial pipeline: preprocess -> train -> evaluate
        _run_preprocess(roar_cli, git_commit, python_exe)
        _run_train(roar_cli, git_commit, python_exe)
        _run_evaluate(roar_cli, git_commit, python_exe)
        _rerun_preprocess_with_modified_input(
            temp_git_repo,
            roar_cli,
            git_commit,
            python_exe,
            "id,value\n1,new_data\n2,changed\n",
        )

        # Check expanded view - artifacts from superseded executions should be superseded
        result = roar_cli("dag", "--expanded", "--show-artifacts", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["is_expanded"] is True

        # Find all superseded steps
        superseded_steps = [n for n in dag_data["nodes"] if n["state"] == "superseded"]
        # Should have at least the old preprocess execution
        assert len(superseded_steps) >= 1

        # Find the old preprocess step (superseded) and its step number
        old_preprocess = next(
            (n for n in superseded_steps if "preprocess.py" in n["command"]), None
        )
        assert old_preprocess is not None

        # In expanded view, the old train and evaluate steps that depend on the
        # superseded preprocess should also be marked as superseded
        old_train = next((n for n in superseded_steps if "train.py" in n["command"]), None)
        old_evaluate = next((n for n in superseded_steps if "evaluate.py" in n["command"]), None)

        # These downstream steps should be marked superseded due to propagation
        assert old_train is not None, (
            "train step downstream of superseded preprocess should be superseded"
        )
        assert old_evaluate is not None, (
            "evaluate step downstream of superseded preprocess should be superseded"
        )

        # Verify artifacts from superseded steps are marked superseded
        superseded_artifacts = [a for a in dag_data["artifacts"] if a["state"] == "superseded"]
        # Should have superseded artifacts (from the old execution path)
        assert len(superseded_artifacts) >= 1
        assert dag_data["superseded_artifact_count"] >= 1
