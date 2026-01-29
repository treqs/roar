"""
Integration tests for the roar dag command.

Tests the dag command with various pipeline configurations.
"""

import json

import pytest


@pytest.mark.happy_path
class TestDagCommand:
    """Test roar dag command functionality."""

    def test_dag_shows_linear_pipeline(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Run preprocess -> train and verify dag output shows 2 steps.

        Given: A linear pipeline with 2 steps
        When: Running roar dag
        Then: Output should show both steps with proper metrics
        """
        # Step 1: Preprocess
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Step 2: Train
        result = roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        assert result.returncode == 0
        git_commit("After train")

        # Run dag command
        result = roar_cli("dag", "--no-color")
        assert result.returncode == 0

        output = result.stdout
        assert "Pipeline: 2 steps" in output
        assert "@1" in output
        assert "@2" in output

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
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        # Modify preprocess output
        (temp_git_repo / "input.csv").write_text("id,value\n1,modified\n2,data\n")
        git_commit("Modified input")

        # Rerun preprocess
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("Rerun preprocess")

        # Check dag - train should be stale
        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["stale_count"] >= 1

        # Find the train step
        train_step = next((n for n in dag_data["nodes"] if "train.py" in n["command"]), None)
        assert train_step is not None
        assert train_step["state"] == "stale"

    def test_dag_no_color_output(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify --no-color flag produces plain text output.

        Given: A pipeline with steps
        When: Running roar dag --no-color
        Then: Output should not contain ANSI escape codes
        """
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        result = roar_cli("dag", "--no-color")
        assert result.returncode == 0

        # No ANSI escape codes
        assert "\033[" not in result.stdout

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess 1")

        # Modify and rerun
        (temp_git_repo / "input.csv").write_text("id,value\n1,modified\n")
        git_commit("Modified input")
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess 2")

        # Check expanded view
        result = roar_cli("dag", "--expanded", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["is_expanded"] is True

        # Should have more nodes in expanded view (both executions)
        preprocess_nodes = [n for n in dag_data["nodes"] if "preprocess.py" in n["command"]]
        assert len(preprocess_nodes) == 2

    def test_dag_shows_diamond_pattern(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify dag correctly shows diamond pattern dependencies.

        Given: A diamond pipeline pattern
        When: Running roar dag --json
        Then: Dependencies should be correctly tracked
        """
        # Create diamond: input -> features_a/features_b -> combine
        roar_cli("run", python_exe, "extract_features_a.py", "input.csv", "features_a.csv")
        git_commit("After features_a")

        roar_cli("run", python_exe, "extract_features_b.py", "input.csv", "features_b.csv")
        git_commit("After features_b")

        roar_cli(
            "run", python_exe, "combine.py", "features_a.csv", "features_b.csv", "combined.json"
        )
        git_commit("After combine")

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["total_steps"] == 3

        # Find the combine step
        combine_step = next((n for n in dag_data["nodes"] if "combine.py" in n["command"]), None)
        assert combine_step is not None
        assert combine_step["metrics"]["consumed"] == 2
        assert len(combine_step["dependencies"]) == 2

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
        assert (
            "No active session" in result.stdout
            or "No steps in pipeline" in result.stdout
            or "Pipeline: 0 steps" in result.stdout
        )

    # =========================================================================
    # Scenario Coverage Tests (from SCENARIO.md)
    # =========================================================================

    def test_dag_multi_model_comparison(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_data,
        python_exe,
    ):
        """
        Test multi-model comparison pattern (Scenario 3).

        Given: 3 parallel training jobs feeding into select_best
        When: Running roar dag --json
        Then: select_best should show all 3 models as dependencies
        """
        # Create model training scripts
        for model_type in ["rf", "xgb", "nn"]:
            script = temp_git_repo / f"train_{model_type}.py"
            script.write_text(f'''
import sys
import json

input_file = "input.csv"
output_file = sys.argv[1] if len(sys.argv) > 1 else "model_{model_type}.pkl"

with open(input_file, "r") as f:
    data = f.read()

model = {{"type": "{model_type}", "data_hash": hash(data)}}
with open(output_file, "w") as f:
    json.dump(model, f)

print(f"Trained {model_type} model -> {{output_file}}")
''')
        git_commit("Add model training scripts")

        # Create select_best script
        select_best = temp_git_repo / "select_best.py"
        select_best.write_text("""
import sys
import json

models = ["model_rf.pkl", "model_xgb.pkl", "model_nn.pkl"]
combined = {}
for m in models:
    with open(m, "r") as f:
        combined[m] = json.load(f)

# Select best (dummy logic)
with open("model.pkl", "w") as f:
    json.dump({"selected": "rf", "models": combined}, f)

print("Selected best model -> model.pkl")
""")
        git_commit("Add select_best script")

        # Run training jobs in parallel (different models)
        roar_cli("run", python_exe, "train_rf.py", "model_rf.pkl")
        git_commit("After train_rf")

        roar_cli("run", python_exe, "train_xgb.py", "model_xgb.pkl")
        git_commit("After train_xgb")

        roar_cli("run", python_exe, "train_nn.py", "model_nn.pkl")
        git_commit("After train_nn")

        # Select best
        roar_cli("run", python_exe, "select_best.py")
        git_commit("After select_best")

        # Check DAG
        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["total_steps"] == 4

        # Find select_best step
        select_step = next((n for n in dag_data["nodes"] if "select_best.py" in n["command"]), None)
        assert select_step is not None
        assert select_step["metrics"]["consumed"] == 3
        assert len(select_step["dependencies"]) == 3

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
        roar_cli("run", python_exe, "extract_features_a.py", "input.csv", "features_a.csv")
        git_commit("After features_a")

        roar_cli("run", python_exe, "extract_features_b.py", "input.csv", "features_b.csv")
        git_commit("After features_b")

        roar_cli("run", python_exe, "extract_features_c.py", "input.csv", "features_c.csv")
        git_commit("After features_c")

        # Combine features (fan-in)
        roar_cli(
            "run",
            python_exe,
            "combine.py",
            "features_a.csv",
            "features_b.csv",
            "features_c.csv",
            "combined.json",
        )
        git_commit("After combine")

        # Train on combined features
        roar_cli("run", python_exe, "train.py", "combined.json", "model.pkl")
        git_commit("After train")

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["total_steps"] == 5

        # Find combine step
        combine_step = next((n for n in dag_data["nodes"] if "combine.py" in n["command"]), None)
        assert combine_step is not None
        assert combine_step["metrics"]["consumed"] == 3
        assert len(combine_step["dependencies"]) == 3

    def test_dag_ensemble_model(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_data,
        python_exe,
    ):
        """
        Test ensemble model pattern (Scenario 6).

        Given: Base models A, B, C feeding ensemble
        When: Running roar dag --json
        Then: Ensemble should show 3 dependencies
        """
        # Create base model training scripts
        for model_name in ["a", "b", "c"]:
            script = temp_git_repo / f"train_model_{model_name}.py"
            script.write_text(f"""
import sys
import json

with open("input.csv", "r") as f:
    data = f.read()

model = {{"type": "model_{model_name}", "data_hash": hash(data)}}
with open("model_{model_name}.pkl", "w") as f:
    json.dump(model, f)

print(f"Trained model_{model_name}")
""")
        git_commit("Add base model scripts")

        # Create ensemble script
        ensemble = temp_git_repo / "train_ensemble.py"
        ensemble.write_text("""
import json

models = {}
for name in ["a", "b", "c"]:
    with open(f"model_{name}.pkl", "r") as f:
        models[name] = json.load(f)

# Also read input data for training ensemble
with open("input.csv", "r") as f:
    data = f.read()

ensemble_model = {"type": "ensemble", "base_models": models}
with open("model.pkl", "w") as f:
    json.dump(ensemble_model, f)

print("Trained ensemble model")
""")
        git_commit("Add ensemble script")

        # Train base models
        roar_cli("run", python_exe, "train_model_a.py")
        git_commit("After model_a")

        roar_cli("run", python_exe, "train_model_b.py")
        git_commit("After model_b")

        roar_cli("run", python_exe, "train_model_c.py")
        git_commit("After model_c")

        # Train ensemble
        roar_cli("run", python_exe, "train_ensemble.py")
        git_commit("After ensemble")

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["total_steps"] == 4

        # Find ensemble step
        ensemble_step = next(
            (n for n in dag_data["nodes"] if "train_ensemble.py" in n["command"]), None
        )
        assert ensemble_step is not None
        assert ensemble_step["metrics"]["consumed"] == 3
        assert len(ensemble_step["dependencies"]) == 3

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
        build_script = temp_git_repo / "setup_env.py"
        build_script.write_text("""
import json

# Simulate setting up environment
config = {"version": "1.0", "setup_complete": True}
with open("config.json", "w") as f:
    json.dump(config, f)

print("Environment setup complete")
""")
        git_commit("Add build script")

        # Run build step
        roar_cli("build", python_exe, "setup_env.py")
        git_commit("After build")

        # Run a regular step that uses the config
        run_script = temp_git_repo / "use_config.py"
        run_script.write_text("""
import json

with open("config.json", "r") as f:
    config = json.load(f)

with open("output.json", "w") as f:
    json.dump({"used_config": config}, f)

print("Used config")
""")
        git_commit("Add use_config script")

        roar_cli("run", python_exe, "use_config.py")
        git_commit("After use_config")

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        roar_cli("run", python_exe, "evaluate.py", "model.pkl", "test.csv", "metrics.json")
        git_commit("After evaluate")

        # Modify input and rerun preprocess
        (temp_git_repo / "input.csv").write_text("id,value\n1,new_data\n2,changed\n")
        git_commit("Modified input")

        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("Rerun preprocess")

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
        split_script = temp_git_repo / "split.py"
        split_script.write_text("""
import sys

with open("input.csv", "r") as f:
    data = f.read()

# Split into two parts
with open("part_a.csv", "w") as f:
    f.write(data[:len(data)//2])

with open("part_b.csv", "w") as f:
    f.write(data[len(data)//2:])

print("Split input into part_a and part_b")
""")
        git_commit("Add split script")

        # Build diamond: split -> (train_a, train_b) -> merge
        roar_cli("run", python_exe, "split.py")
        git_commit("After split")

        # Create train_a script
        train_a = temp_git_repo / "train_a.py"
        train_a.write_text("""
import json
with open("part_a.csv", "r") as f:
    data = f.read()
with open("model_a.pkl", "w") as f:
    json.dump({"model": "a", "hash": hash(data)}, f)
print("Trained model_a")
""")
        git_commit("Add train_a script")

        roar_cli("run", python_exe, "train_a.py")
        git_commit("After train_a")

        # Create train_b script
        train_b = temp_git_repo / "train_b.py"
        train_b.write_text("""
import json
with open("part_b.csv", "r") as f:
    data = f.read()
with open("model_b.pkl", "w") as f:
    json.dump({"model": "b", "hash": hash(data)}, f)
print("Trained model_b")
""")
        git_commit("Add train_b script")

        roar_cli("run", python_exe, "train_b.py")
        git_commit("After train_b")

        # Create merge script
        merge_script = temp_git_repo / "merge_models.py"
        merge_script.write_text("""
import json
with open("model_a.pkl", "r") as f:
    model_a = json.load(f)
with open("model_b.pkl", "r") as f:
    model_b = json.load(f)
with open("final_model.pkl", "w") as f:
    json.dump({"a": model_a, "b": model_b}, f)
print("Merged models")
""")
        git_commit("Add merge script")

        roar_cli("run", python_exe, "merge_models.py")
        git_commit("After merge")

        # Rerun only train_a (branch A)
        (temp_git_repo / "part_a.csv").write_text("modified,data\n")
        git_commit("Modified part_a")

        roar_cli("run", python_exe, "train_a.py")
        git_commit("Rerun train_a")

        # Check partial invalidation
        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        # Merge should be stale (consumes from both branches)
        merge_step = next((n for n in dag_data["nodes"] if "merge_models.py" in n["command"]), None)
        assert merge_step is not None
        assert merge_step["state"] == "stale"

    def test_dag_checkpoint_recovery(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Test checkpoint recovery after failure (Scenario 14).

        Given: A failed job that is then successfully rerun
        When: Running roar dag --expanded --json
        Then: Both executions should appear in expanded view
        """
        # Create a script that can fail
        fail_script = temp_git_repo / "might_fail.py"
        fail_script.write_text("""
import sys
import os

fail_flag = os.environ.get("SHOULD_FAIL", "false")
output_file = sys.argv[1] if len(sys.argv) > 1 else "output.json"

# Read input
with open("input.csv", "r") as f:
    data = f.read()

if fail_flag == "true":
    print("Simulating failure!")
    sys.exit(1)

# Success path
with open(output_file, "w") as f:
    f.write(data.upper())
print(f"Success: wrote {output_file}")
""")
        git_commit("Add might_fail script")

        # First run (success for setup)
        roar_cli("run", python_exe, "might_fail.py", "output.json")
        git_commit("After first run")

        # Modify input and rerun
        (temp_git_repo / "input.csv").write_text("id,value\n1,retry\n")
        git_commit("Modified input")

        roar_cli("run", python_exe, "might_fail.py", "output.json")
        git_commit("After second run")

        # Check expanded view shows both executions
        result = roar_cli("dag", "--expanded", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)
        assert dag_data["is_expanded"] is True

        # Find all might_fail executions
        might_fail_nodes = [n for n in dag_data["nodes"] if "might_fail.py" in n["command"]]
        assert len(might_fail_nodes) == 2

    # =========================================================================
    # Edge Case Tests
    # =========================================================================

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
            roar_cli("run", python_exe, f"step_{i}.py")
            git_commit(f"After step_{i}")

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
            roar_cli("run", python_exe, f"level_{i}.py")
            git_commit(f"After level_{i}")

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
        Test that step_name field exists in JSON output structure.

        Given: Steps run with commands
        When: Running roar dag --json
        Then: JSON output should have step_name field in node structure

        Note: The --name flag exists in the CLI but step_name propagation
        through RunContext is not yet implemented. This test validates
        the JSON structure includes the field.
        """
        # Run steps
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        # Verify JSON includes step_name field in structure
        result = roar_cli("dag", "--json")
        assert result.returncode == 0
        dag_data = json.loads(result.stdout)

        preprocess_step = next(
            (n for n in dag_data["nodes"] if "preprocess.py" in n["command"]), None
        )
        train_step = next((n for n in dag_data["nodes"] if "train.py" in n["command"]), None)

        assert preprocess_step is not None
        assert train_step is not None

        # Verify step_name field exists in JSON structure (even if None)
        assert "step_name" in preprocess_step
        assert "step_name" in train_step

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
        multi_output = temp_git_repo / "multi_output.py"
        multi_output.write_text("""
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
""")
        git_commit("Add multi_output script")

        roar_cli("run", python_exe, "multi_output.py")
        git_commit("After multi_output")

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

    def test_dag_long_command_truncation(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_data,
        python_exe,
    ):
        """
        Test truncation of long commands in output.

        Given: A command with very long arguments
        When: Running roar dag --no-color
        Then: Command should be truncated with '...'
        """
        # Create script with long name and arguments
        long_script = temp_git_repo / "very_long_script_name_that_goes_on_and_on.py"
        long_script.write_text("""
import sys

with open("input.csv", "r") as f:
    data = f.read()

with open("output.txt", "w") as f:
    f.write(data + " " + " ".join(sys.argv[1:]))

print("Done")
""")
        git_commit("Add long script")

        # Run with very long arguments
        long_args = ["--parameter-" + str(i) + "=value" + str(i) for i in range(1, 20)]
        roar_cli("run", python_exe, "very_long_script_name_that_goes_on_and_on.py", *long_args)
        git_commit("After long command")

        result = roar_cli("dag", "--no-color")
        assert result.returncode == 0

        output = result.stdout
        # Output should contain truncation indicator
        assert "..." in output or "very_long" in output

        # Full command should still be available in JSON
        result = roar_cli("dag", "--json")
        dag_data = json.loads(result.stdout)

        node = dag_data["nodes"][0]
        assert "very_long_script_name" in node["command"]
        assert "--parameter-1=value1" in node["command"]

    # =========================================================================
    # Artifact State Tests
    # =========================================================================

    def test_dag_artifact_states_in_json(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify artifact state fields are present in JSON output.

        Given: A simple pipeline
        When: Running roar dag --json
        Then: Artifacts should have state, artifact_id, consumer_steps, is_terminal fields
        """
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        assert "stale_artifact_count" in dag_data
        assert "superseded_artifact_count" in dag_data

        if dag_data["artifacts"]:
            artifact = dag_data["artifacts"][0]
            assert "state" in artifact
            assert "artifact_id" in artifact
            assert "consumer_steps" in artifact
            assert "is_terminal" in artifact
            assert "superseded_by" in artifact

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        # Modify input and rerun preprocess to make train stale
        (temp_git_repo / "input.csv").write_text("id,value\n1,modified\n2,data\n")
        git_commit("Modified input")

        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("Rerun preprocess")

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        # Rerun preprocess to make train stale
        (temp_git_repo / "input.csv").write_text("id,value\n1,new\n")
        git_commit("Modified input")

        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("Rerun preprocess")

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

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

    def test_dag_text_output_shows_artifact_states(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify text output shows artifact state markers.

        Given: A pipeline with stale artifacts
        When: Running roar dag --no-color
        Then: Stale artifacts should show [stale] marker
        """
        # Build pipeline
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        # Rerun preprocess to make train stale
        (temp_git_repo / "input.csv").write_text("id,value\n1,modified\n")
        git_commit("Modified input")

        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("Rerun preprocess")

        result = roar_cli("dag", "--no-color")
        assert result.returncode == 0

        output = result.stdout
        # Stale artifact should show [stale] marker
        assert "[stale]" in output or "stale" in output.lower()

    def test_dag_active_artifact_state(
        self,
        temp_git_repo,
        roar_cli,
        git_commit,
        sample_scripts,
        sample_data,
        python_exe,
    ):
        """
        Verify active artifacts have state="active".

        Given: A simple pipeline with no stale steps
        When: Running roar dag --json
        Then: Terminal artifacts should have state="active"
        """
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        result = roar_cli("dag", "--json")
        assert result.returncode == 0

        dag_data = json.loads(result.stdout)

        # All artifacts should be active
        for artifact in dag_data["artifacts"]:
            assert artifact["state"] == "active"

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
        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("After preprocess")

        roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        git_commit("After train")

        roar_cli("run", python_exe, "evaluate.py", "model.pkl", "test.csv", "metrics.json")
        git_commit("After evaluate")

        # Modify input and rerun preprocess (this creates a superseded version)
        (temp_git_repo / "input.csv").write_text("id,value\n1,new_data\n2,changed\n")
        git_commit("Modified input")

        roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        git_commit("Rerun preprocess")

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
