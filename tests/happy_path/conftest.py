"""
Happy path test fixtures.

Provides reusable sample scripts and data for testing roar run scenarios.
"""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def python_exe() -> str:
    """
    Return the absolute path to the Python executable.

    roar run uses a sandboxed execution environment that doesn't have
    'python' in PATH, so we need to use absolute paths.
    """
    return sys.executable


@pytest.fixture
def sample_scripts(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """
    Create reusable sample Python scripts.

    Creates scripts for:
    - preprocess: Reads input.csv, writes processed.csv
    - train: Reads processed.csv, writes model.pkl
    - evaluate: Reads model.pkl and test.csv, writes metrics.json
    - combine: Reads multiple inputs, writes combined output
    - extract_*: Feature extraction scripts for fan-out patterns

    Returns:
        Dictionary mapping script name to Path
    """
    scripts = {}

    # Preprocess script
    preprocess = temp_git_repo / "preprocess.py"
    preprocess.write_text("""
import sys

# Read input
input_file = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "processed.csv"

with open(input_file, "r") as f:
    data = f.read()

# Process (simple transformation)
processed = data.upper()

with open(output_file, "w") as f:
    f.write(processed)

print(f"Processed {input_file} -> {output_file}")
""")
    scripts["preprocess"] = preprocess

    # Train script
    train = temp_git_repo / "train.py"
    train.write_text("""
import sys
import json

input_file = sys.argv[1] if len(sys.argv) > 1 else "processed.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "model.pkl"

# Parse optional hyperparameters
lr = 0.01
epochs = 10
for arg in sys.argv[1:]:
    if arg.startswith("--lr="):
        lr = float(arg.split("=")[1])
    elif arg.startswith("--epochs="):
        epochs = int(arg.split("=")[1])

with open(input_file, "r") as f:
    data = f.read()

# Simulate training
model = {"data_hash": hash(data), "lr": lr, "epochs": epochs}

with open(output_file, "w") as f:
    json.dump(model, f)

print(f"Trained model from {input_file} -> {output_file}")
print(f"  lr={lr}, epochs={epochs}")
""")
    scripts["train"] = train

    # Evaluate script
    evaluate = temp_git_repo / "evaluate.py"
    evaluate.write_text("""
import sys
import json

model_file = sys.argv[1] if len(sys.argv) > 1 else "model.pkl"
test_file = sys.argv[2] if len(sys.argv) > 2 else "test.csv"
output_file = sys.argv[3] if len(sys.argv) > 3 else "metrics.json"

with open(model_file, "r") as f:
    model = json.load(f)

with open(test_file, "r") as f:
    test_data = f.read()

# Simulate evaluation
metrics = {
    "accuracy": 0.95,
    "loss": 0.05,
    "model_epochs": model.get("epochs", "?"),
}

with open(output_file, "w") as f:
    json.dump(metrics, f, indent=2)

print(f"Evaluated {model_file} on {test_file} -> {output_file}")
""")
    scripts["evaluate"] = evaluate

    # Combine script (for diamond/fan-in patterns)
    combine = temp_git_repo / "combine.py"
    combine.write_text("""
import sys
import json

output_file = sys.argv[-1] if len(sys.argv) > 1 else "combined.json"
input_files = sys.argv[1:-1] if len(sys.argv) > 2 else []

combined = {}
for i, f in enumerate(input_files):
    with open(f, "r") as fp:
        combined[f"input_{i}"] = fp.read()

with open(output_file, "w") as f:
    json.dump(combined, f, indent=2)

print(f"Combined {len(input_files)} inputs -> {output_file}")
""")
    scripts["combine"] = combine

    # Feature extraction scripts (for fan-out patterns)
    for name in ["features_a", "features_b", "features_c"]:
        script = temp_git_repo / f"extract_{name}.py"
        script.write_text(f'''
import sys

input_file = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "{name}.csv"

with open(input_file, "r") as f:
    data = f.read()

# Extract features (simulated)
features = f"{name.upper()}: " + data[:100]

with open(output_file, "w") as f:
    f.write(features)

print(f"Extracted {name} from {{input_file}} -> {{output_file}}")
''')
        scripts[f"extract_{name}"] = script

    # Commit the scripts
    git_commit("Add sample scripts")

    return scripts


@pytest.fixture
def sample_data(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """
    Create sample input data files.

    Returns:
        Dictionary mapping data name to Path
    """
    data_files = {}

    # Input CSV
    input_csv = temp_git_repo / "input.csv"
    input_csv.write_text("id,value\n1,foo\n2,bar\n3,baz\n")
    data_files["input"] = input_csv

    # Test CSV
    test_csv = temp_git_repo / "test.csv"
    test_csv.write_text("id,value\n4,qux\n5,quux\n")
    data_files["test"] = test_csv

    # Commit the data
    git_commit("Add sample data")

    return data_files
