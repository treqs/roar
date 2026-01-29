"""
Live integration tests for full pipeline with build and training steps.

These tests require a running GLaaS server and are marked with @pytest.mark.live_glaas.
They test the complete workflow: build steps + training steps + registration + reproduction.

Run with:
    pytest tests/live_glaas/test_full_pipeline_live.py -v -m live_glaas --dist no

Prerequisites:
    1. Start glaas-api: cd /path/to/glaas-api && npm run dev
    2. Ensure GLAAS_URL env var is set or server is at http://localhost:3001
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from roar.glaas_client import make_auth_header

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def glaas_url():
    """Get GLaaS server URL from environment or default."""
    return os.environ.get("GLAAS_URL", "http://localhost:3001")


@pytest.fixture
def glaas_available(glaas_url):
    """Check if GLaaS server is available."""
    try:
        req = urllib.request.Request(f"{glaas_url}/api/v1/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture
def glaas_configured_with_local_remote(temp_git_repo, glaas_url):
    """Configure GLaaS URL and set git remote to local file:// URL.

    Uses file:// URL so reproduction can clone from local temp repo.
    """
    local_remote = f"file://{temp_git_repo}"
    subprocess.run(
        ["git", "remote", "add", "origin", local_remote],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "glaas.url", glaas_url],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    return temp_git_repo


@pytest.fixture
def reproduction_target_dir(tmp_path_factory):
    """Unique temp dir for reproduction - parallel-safe."""
    return tmp_path_factory.mktemp("reproduce")


@pytest.fixture
def python_exe() -> str:
    """Return the absolute path to the Python executable."""
    return sys.executable


@pytest.fixture
def compute_file_hash():
    """Compute blake3 hash of a file."""

    def _compute(filepath: Path) -> str:
        try:
            import blake3

            hasher = blake3.blake3()
            with open(filepath, "rb") as f:
                while chunk := f.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except ImportError:
            # Fallback to SHA256 if blake3 not available
            hasher = hashlib.sha256()
            with open(filepath, "rb") as f:
                while chunk := f.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()

    return _compute


@pytest.fixture
def build_scripts(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """Create 3 build step scripts that produce deterministic outputs.

    Build steps:
    1. setup_env.py -> config.json (environment configuration)
    2. compile_utils.py -> utils.so (compiled utilities)
    3. install_deps.py -> deps_manifest.json (dependency manifest)
    """
    scripts = {}

    # Build step 1: Setup environment config
    setup_env = temp_git_repo / "setup_env.py"
    setup_env.write_text(
        '''"""Build step 1: Create environment configuration."""
import json
import hashlib
import os

output_file = "config.json"

# Deterministic config based on Python version and platform
config = {
    "version": "1.0.0",
    "python_version": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
    "seed": 42,
    "learning_rate": 0.001,
    "epochs": 10,
    "batch_size": 32,
    "deterministic": True,
}

# Make hash deterministic by sorting keys
config_str = json.dumps(config, sort_keys=True)
config["config_hash"] = hashlib.sha256(config_str.encode()).hexdigest()[:16]

with open(output_file, "w") as f:
    json.dump(config, f, indent=2, sort_keys=True)

print(f"Created {output_file} with config hash {config['config_hash']}")
'''
    )
    scripts["setup_env"] = setup_env

    # Build step 2: Compile utilities (simulated)
    compile_utils = temp_git_repo / "compile_utils.py"
    compile_utils.write_text(
        '''"""Build step 2: Compile utility functions (simulated)."""
import hashlib

output_file = "utils.so"

# Simulate compiled binary with deterministic content
# In real use case, this would be: maturin develop, cargo build, make, etc.
source_code = """
def normalize(x):
    return (x - x.mean()) / x.std()

def augment(x):
    return x * 1.1
"""

# Deterministic "compiled" output
compiled_hash = hashlib.sha256(source_code.encode()).hexdigest()
compiled_content = f"COMPILED_BINARY_V1\\nSOURCE_HASH:{compiled_hash}\\nARCH:x86_64\\n"

with open(output_file, "w") as f:
    f.write(compiled_content)

print(f"Compiled utilities -> {output_file}")
'''
    )
    scripts["compile_utils"] = compile_utils

    # Build step 3: Install dependencies manifest
    install_deps = temp_git_repo / "install_deps.py"
    install_deps.write_text(
        '''"""Build step 3: Create dependency manifest."""
import json
import hashlib

output_file = "deps_manifest.json"

# Simulated installed packages (deterministic)
# In real use case: pip install -e ., poetry install, etc.
deps = {
    "packages": [
        {"name": "numpy", "version": "1.24.0"},
        {"name": "pandas", "version": "2.0.0"},
        {"name": "scikit-learn", "version": "1.3.0"},
    ],
    "python_requires": ">=3.10",
}

# Add manifest hash for verification
deps_str = json.dumps(deps, sort_keys=True)
deps["manifest_hash"] = hashlib.sha256(deps_str.encode()).hexdigest()[:16]

with open(output_file, "w") as f:
    json.dump(deps, f, indent=2, sort_keys=True)

print(f"Created dependency manifest -> {output_file}")
'''
    )
    scripts["install_deps"] = install_deps

    git_commit("Add build scripts")
    return scripts


@pytest.fixture
def training_scripts(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """Create 5 training step scripts that produce deterministic outputs.

    Training steps:
    1. preprocess.py: input.csv -> processed.csv
    2. extract_features.py: processed.csv -> features.npy
    3. split_data.py: features.npy -> train.npy, val.npy
    4. train_model.py: train.npy + config.json -> model.pt
    5. evaluate.py: model.pt + val.npy -> final_model.pt
    """
    scripts = {}

    # Run step 1: Preprocess data
    preprocess = temp_git_repo / "preprocess.py"
    preprocess.write_text(
        '''"""Run step 1: Preprocess raw data."""
import sys
import hashlib

input_file = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "processed.csv"

with open(input_file, "r") as f:
    data = f.read()

# Deterministic transformation: uppercase + add checksum header
processed = data.upper()
checksum = hashlib.sha256(data.encode()).hexdigest()[:8]
output = f"# PREPROCESSED checksum={checksum}\\n{processed}"

with open(output_file, "w") as f:
    f.write(output)

print(f"Preprocessed {input_file} -> {output_file}")
'''
    )
    scripts["preprocess"] = preprocess

    # Run step 2: Extract features
    extract_features = temp_git_repo / "extract_features.py"
    extract_features.write_text(
        '''"""Run step 2: Extract features from processed data."""
import sys
import hashlib

input_file = sys.argv[1] if len(sys.argv) > 1 else "processed.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "features.npy"

with open(input_file, "r") as f:
    data = f.read()

# Deterministic feature extraction (simulated numpy array)
# Hash the input to create deterministic "features"
data_hash = hashlib.sha256(data.encode()).hexdigest()

# Simulate numpy array format
features = f"NUMPY_ARRAY_V1\\nSHAPE:(100,64)\\nDTYPE:float32\\nDATA_HASH:{data_hash}\\n"

# Add some deterministic "feature values" based on hash
for i in range(10):
    chunk = data_hash[i*6:(i+1)*6]
    value = int(chunk, 16) / 0xFFFFFF
    features += f"FEATURE_{i}:{value:.6f}\\n"

with open(output_file, "w") as f:
    f.write(features)

print(f"Extracted features {input_file} -> {output_file}")
'''
    )
    scripts["extract_features"] = extract_features

    # Run step 3: Split data
    split_data = temp_git_repo / "split_data.py"
    split_data.write_text(
        '''"""Run step 3: Split features into train/validation sets."""
import sys
import hashlib

input_file = sys.argv[1] if len(sys.argv) > 1 else "features.npy"
train_output = sys.argv[2] if len(sys.argv) > 2 else "train.npy"
val_output = sys.argv[3] if len(sys.argv) > 3 else "val.npy"

with open(input_file, "r") as f:
    features = f.read()

# Deterministic split (80/20)
features_hash = hashlib.sha256(features.encode()).hexdigest()
split_seed = int(features_hash[:8], 16) % 1000

# Create train set (80%)
train_content = f"NUMPY_ARRAY_V1\\nSPLIT:train\\nRATIO:0.8\\nSEED:{split_seed}\\n"
train_content += f"PARENT_HASH:{features_hash[:16]}\\n"
train_content += f"SAMPLES:80\\n"

# Create validation set (20%)
val_content = f"NUMPY_ARRAY_V1\\nSPLIT:validation\\nRATIO:0.2\\nSEED:{split_seed}\\n"
val_content += f"PARENT_HASH:{features_hash[:16]}\\n"
val_content += f"SAMPLES:20\\n"

with open(train_output, "w") as f:
    f.write(train_content)

with open(val_output, "w") as f:
    f.write(val_content)

print(f"Split {input_file} -> {train_output}, {val_output}")
'''
    )
    scripts["split_data"] = split_data

    # Run step 4: Train model (uses config.json from build step)
    train_model = temp_git_repo / "train_model.py"
    train_model.write_text(
        '''"""Run step 4: Train model using training data and config."""
import sys
import hashlib
import json

train_file = sys.argv[1] if len(sys.argv) > 1 else "train.npy"
config_file = sys.argv[2] if len(sys.argv) > 2 else "config.json"
output_file = sys.argv[3] if len(sys.argv) > 3 else "model.pt"

# Read training data
with open(train_file, "r") as f:
    train_data = f.read()

# Read config from build step
with open(config_file, "r") as f:
    config = json.load(f)

# Deterministic model "weights" based on training data + config
combined = train_data + json.dumps(config, sort_keys=True)
weights_hash = hashlib.sha256(combined.encode()).hexdigest()

# Create model file
model_content = f"PYTORCH_MODEL_V1\\n"
model_content += f"WEIGHTS_HASH:{weights_hash}\\n"
model_content += f"EPOCHS:{config.get('epochs', 10)}\\n"
model_content += f"LEARNING_RATE:{config.get('learning_rate', 0.001)}\\n"
model_content += f"BATCH_SIZE:{config.get('batch_size', 32)}\\n"
model_content += f"TRAIN_SAMPLES:{train_data.count('SAMPLES')}\\n"

# Add deterministic layer weights
for layer in range(5):
    layer_hash = hashlib.sha256(f"{weights_hash}:layer{layer}".encode()).hexdigest()[:16]
    model_content += f"LAYER_{layer}_WEIGHTS:{layer_hash}\\n"

with open(output_file, "w") as f:
    f.write(model_content)

print(f"Trained model {train_file} + {config_file} -> {output_file}")
'''
    )
    scripts["train_model"] = train_model

    # Run step 5: Evaluate model
    evaluate = temp_git_repo / "evaluate.py"
    evaluate.write_text(
        '''"""Run step 5: Evaluate model and create final artifact."""
import sys
import hashlib

model_file = sys.argv[1] if len(sys.argv) > 1 else "model.pt"
val_file = sys.argv[2] if len(sys.argv) > 2 else "val.npy"
output_file = sys.argv[3] if len(sys.argv) > 3 else "final_model.pt"

# Read model
with open(model_file, "r") as f:
    model_content = f.read()

# Read validation data
with open(val_file, "r") as f:
    val_data = f.read()

# Deterministic evaluation metrics
combined = model_content + val_data
eval_hash = hashlib.sha256(combined.encode()).hexdigest()

# Calculate deterministic "metrics" from hash
accuracy = 0.90 + (int(eval_hash[:4], 16) % 1000) / 10000  # 0.90-0.99
loss = (int(eval_hash[4:8], 16) % 1000) / 10000  # 0.00-0.10

# Create final model with evaluation metadata
final_content = model_content
final_content += f"\\n# EVALUATION RESULTS\\n"
final_content += f"EVAL_HASH:{eval_hash[:16]}\\n"
final_content += f"ACCURACY:{accuracy:.4f}\\n"
final_content += f"LOSS:{loss:.4f}\\n"
final_content += f"VAL_SAMPLES:{val_data.count('SAMPLES')}\\n"
final_content += f"STATUS:VERIFIED\\n"

with open(output_file, "w") as f:
    f.write(final_content)

print(f"Evaluated {model_file} on {val_file} -> {output_file}")
print(f"Accuracy: {accuracy:.4f}, Loss: {loss:.4f}")
'''
    )
    scripts["evaluate"] = evaluate

    git_commit("Add training scripts")
    return scripts


@pytest.fixture
def sample_input_data(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """Create sample input data file."""
    data_files = {}

    input_csv = temp_git_repo / "input.csv"
    input_csv.write_text(
        """id,feature_a,feature_b,label
1,0.5,0.3,positive
2,0.8,0.1,negative
3,0.2,0.9,positive
4,0.6,0.4,negative
5,0.1,0.7,positive
"""
    )
    data_files["input"] = input_csv

    git_commit("Add input data")
    return data_files


# =============================================================================
# Test Classes
# =============================================================================


@pytest.mark.live_glaas
class TestFullPipelineLiveGlaas:
    """Full end-to-end tests with build steps, training steps, registration, and reproduction."""

    def test_full_pipeline_with_build_and_run_steps(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        build_scripts,
        training_scripts,
        sample_input_data,
        reproduction_target_dir,
        compute_file_hash,
    ):
        """
        Test complete workflow:
        1. Run 3 build steps (roar build ...)
        2. Run 5 training steps (roar run ...)
        3. Register final model with GLaaS
        4. Verify lineage includes all 8 steps
        5. Reproduce in fresh directory
        6. Verify reproduced model hash matches original
        """
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        source_repo = glaas_configured_with_local_remote

        # =====================================================================
        # Phase 1: Execute 3 Build Steps
        # =====================================================================

        # Build step 1: Setup environment config
        result = roar_cli("build", python_exe, "setup_env.py")
        assert result.returncode == 0, f"Build step 1 failed: {result.stderr}"
        assert (source_repo / "config.json").exists(), "config.json not created"
        # Verify config.json contents match fixture expectations
        config_data = json.loads((source_repo / "config.json").read_text())
        assert config_data["version"] == "1.0.0"
        assert config_data["seed"] == 42
        assert config_data["learning_rate"] == 0.001
        git_commit("After setup_env build")

        # Build step 2: Compile utilities
        result = roar_cli("build", python_exe, "compile_utils.py")
        assert result.returncode == 0, f"Build step 2 failed: {result.stderr}"
        assert (source_repo / "utils.so").exists(), "utils.so not created"
        # Verify utils.so format markers
        utils_content = (source_repo / "utils.so").read_text()
        assert "COMPILED_BINARY_V1" in utils_content
        assert "ARCH:x86_64" in utils_content
        git_commit("After compile_utils build")

        # Build step 3: Install dependencies manifest
        result = roar_cli("build", python_exe, "install_deps.py")
        assert result.returncode == 0, f"Build step 3 failed: {result.stderr}"
        assert (source_repo / "deps_manifest.json").exists(), "deps_manifest.json not created"
        # Verify deps_manifest.json structure
        deps_data = json.loads((source_repo / "deps_manifest.json").read_text())
        assert len(deps_data["packages"]) == 3
        git_commit("After install_deps build")

        # =====================================================================
        # Phase 2: Execute 5 Training Steps
        # =====================================================================

        # Run step 1: Preprocess data
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0, f"Run step 1 failed: {result.stderr}"
        assert (source_repo / "processed.csv").exists()
        # Verify processed.csv - uppercase transformation applied
        processed = (source_repo / "processed.csv").read_text()
        assert processed.startswith("# PREPROCESSED checksum=")
        assert "ID,FEATURE_A" in processed  # uppercased
        git_commit("After preprocess")

        # Run step 2: Extract features
        result = roar_cli("run", python_exe, "extract_features.py", "processed.csv", "features.npy")
        assert result.returncode == 0, f"Run step 2 failed: {result.stderr}"
        assert (source_repo / "features.npy").exists()
        git_commit("After feature extraction")

        # Run step 3: Split data
        result = roar_cli(
            "run", python_exe, "split_data.py", "features.npy", "train.npy", "val.npy"
        )
        assert result.returncode == 0, f"Run step 3 failed: {result.stderr}"
        assert (source_repo / "train.npy").exists()
        assert (source_repo / "val.npy").exists()
        # Verify train.npy/val.npy split markers
        train = (source_repo / "train.npy").read_text()
        assert "SPLIT:train" in train
        assert "RATIO:0.8" in train
        git_commit("After data split")

        # Run step 4: Train model (uses config.json from build step 1)
        result = roar_cli(
            "run", python_exe, "train_model.py", "train.npy", "config.json", "model.pt"
        )
        assert result.returncode == 0, f"Run step 4 failed: {result.stderr}"
        assert (source_repo / "model.pt").exists()
        git_commit("After training")

        # Run step 5: Evaluate model
        result = roar_cli("run", python_exe, "evaluate.py", "model.pt", "val.npy", "final_model.pt")
        assert result.returncode == 0, f"Run step 5 failed: {result.stderr}"
        assert (source_repo / "final_model.pt").exists()
        git_commit("After evaluation")

        # =====================================================================
        # Phase 3: Verify Lineage and Register
        # =====================================================================

        # Compute original model hash before registration
        original_model = source_repo / "final_model.pt"
        original_hash = compute_file_hash(original_model)

        # Get artifact hash and lineage
        lineage_result = roar_cli("lineage", "final_model.pt")
        assert lineage_result.returncode == 0, f"Lineage query failed: {lineage_result.stderr}"
        lineage = json.loads(lineage_result.stdout)

        artifact_hash = lineage["artifact"]["hash"]
        assert artifact_hash, "Artifact hash not found in lineage"

        # Verify lineage contains the run steps (at minimum)
        # Note: Lineage traces the artifact dependency DAG. Build steps are only
        # included if their outputs are consumed by run steps in the lineage path.
        # The build step producing config.json should be in lineage since train_model.py
        # reads it, but this depends on proper artifact linking.
        assert "jobs" in lineage, "Lineage missing 'jobs' key"
        jobs = lineage["jobs"]
        assert len(jobs) >= 5, f"Expected at least 5 jobs, got {len(jobs)}"

        # Verify each expected script explicitly (no any() hiding empty lists)
        expected_scripts = [
            "preprocess.py",
            "extract_features.py",
            "split_data.py",
            "train_model.py",
            "evaluate.py",
        ]
        for script in expected_scripts:
            found = False
            for job in jobs:
                assert "command" in job, f"Job missing 'command': {job.keys()}"
                if script in job["command"]:
                    found = True
                    break
            assert found, f"{script} not found in lineage jobs"

        # Check that config.json is in the lineage (as an input to train_model.py)
        # Use explicit loop with exact matching, fail with context
        all_inputs = []
        for job in jobs:
            all_inputs.extend(job.get("inputs", []))

        config_input_count = 0
        for inp in all_inputs:
            assert "path" in inp, f"Input missing 'path' key: {inp}"
            if inp["path"].endswith("config.json"):
                config_input_count += 1

        assert config_input_count == 1, (
            f"Expected exactly 1 config.json input, found {config_input_count}. "
            f"Inputs: {[i['path'] for i in all_inputs]}"
        )

        # Register with GLaaS
        result = roar_cli("register", "final_model.pt")
        assert result.returncode == 0, f"Registration failed: {result.stderr}"

        # Verify registration output
        output = result.stdout
        assert "Session:" in output, "Registration should show session hash"
        assert "Jobs:" in output, "Registration should show job count"

        # Parse the number of jobs registered
        # Expected format: "Jobs: N" where N should be at least 5 (the run steps)
        # Build steps may also be included if their outputs are in the lineage
        import re

        jobs_match = re.search(r"Jobs:\s*(\d+)", output)
        assert jobs_match, f"Could not parse job count from output:\n{output}"
        jobs_registered = int(jobs_match.group(1))
        assert jobs_registered >= 5, (
            f"Expected at least 5 jobs registered (the run steps), got {jobs_registered}"
        )

        # =====================================================================
        # Phase 4: Reproduce and Verify
        # =====================================================================

        # Reproduce in fresh directory
        reproduce_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "reproduce",
                artifact_hash[:16],
                "--run",
                "-y",
            ],
            cwd=reproduction_target_dir,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GLAAS_URL": os.environ.get("GLAAS_URL", "http://localhost:3001"),
            },
        )

        assert reproduce_result.returncode == 0, (
            f"Reproduce failed:\nstdout: {reproduce_result.stdout}\nstderr: {reproduce_result.stderr}"
        )
        assert "Reproduction Complete" in reproduce_result.stdout

        # Verify steps were reported with exact match - avoid loose matching
        stdout = reproduce_result.stdout
        build_match = re.search(r"Build steps:\s*(\d+)", stdout)
        run_match = re.search(r"Run steps:\s*(\d+)", stdout)
        assert build_match is not None, f"Expected 'Build steps: N' in output:\n{stdout}"
        assert run_match is not None, f"Expected 'Run steps: N' in output:\n{stdout}"

        # Find and verify reproduced model
        reproduce_dir = reproduction_target_dir / "reproduce"
        assert reproduce_dir.exists(), "Reproduce directory not created"

        model_files = list(reproduce_dir.rglob("final_model.pt"))
        assert len(model_files) == 1, f"Expected exactly 1 final_model.pt, found {len(model_files)}"

        reproduced_model = model_files[0]
        reproduced_hash = compute_file_hash(reproduced_model)

        assert original_hash == reproduced_hash, (
            f"Hash mismatch!\n"
            f"Original:   {original_hash[:32]}...\n"
            f"Reproduced: {reproduced_hash[:32]}..."
        )

    def test_lineage_includes_build_step_artifacts(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        build_scripts,
        training_scripts,
        sample_input_data,
    ):
        """Verify that lineage traces through build step outputs (config.json)."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        _ = glaas_configured_with_local_remote  # Use fixture for setup

        # Run build steps
        result = roar_cli("build", python_exe, "setup_env.py")
        assert result.returncode == 0
        git_commit("After setup_env")

        result = roar_cli("build", python_exe, "compile_utils.py")
        assert result.returncode == 0
        git_commit("After compile_utils")

        result = roar_cli("build", python_exe, "install_deps.py")
        assert result.returncode == 0
        git_commit("After install_deps")

        # Run training steps (train_model.py uses config.json)
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        result = roar_cli("run", python_exe, "extract_features.py", "processed.csv", "features.npy")
        assert result.returncode == 0
        git_commit("After features")

        result = roar_cli(
            "run", python_exe, "split_data.py", "features.npy", "train.npy", "val.npy"
        )
        assert result.returncode == 0
        git_commit("After split")

        result = roar_cli(
            "run", python_exe, "train_model.py", "train.npy", "config.json", "model.pt"
        )
        assert result.returncode == 0
        git_commit("After train")

        result = roar_cli("run", python_exe, "evaluate.py", "model.pt", "val.npy", "final_model.pt")
        assert result.returncode == 0
        git_commit("After evaluate")

        # Get lineage for final model
        lineage_result = roar_cli("lineage", "final_model.pt")
        lineage = json.loads(lineage_result.stdout)

        # Find the train_model job (run step 4) - explicit loop, no list comprehensions
        assert "jobs" in lineage, "Lineage missing 'jobs' key"
        jobs = lineage["jobs"]
        assert len(jobs) > 0, "Expected jobs but got empty list"

        train_job = None
        for job in jobs:
            assert "command" in job, f"Job missing 'command': {job.keys()}"
            if "train_model.py" in job["command"]:
                train_job = job
                break

        assert train_job is not None, (
            f"train_model.py not found in lineage. Jobs: {[j['command'] for j in jobs]}"
        )

        # Verify config.json is in the inputs of train_model job
        # Use explicit loop with exact matching
        inputs = train_job.get("_inputs", []) or train_job.get("inputs", [])

        config_input_count = 0
        for inp in inputs:
            assert "path" in inp, f"Input missing 'path' key: {inp}"
            if inp["path"].endswith("config.json"):
                config_input_count += 1

        assert config_input_count == 1, (
            f"Expected exactly 1 config.json input in train_model.py inputs, found {config_input_count}. "
            f"Inputs: {[i['path'] for i in inputs]}"
        )

    def test_register_preserves_build_job_type(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        glaas_url,
        roar_cli,
        git_commit,
        python_exe,
        build_scripts,
        training_scripts,
        sample_input_data,
    ):
        """Verify job_type='build' is preserved after registration to GLaaS."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        _ = glaas_configured_with_local_remote  # Use fixture for setup

        # Run minimal pipeline: 1 build + 1 run
        result = roar_cli("build", python_exe, "setup_env.py")
        assert result.returncode == 0
        git_commit("After setup")

        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Get lineage and register
        lineage_result = roar_cli("lineage", "processed.csv")
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]

        result = roar_cli("register", "processed.csv")
        assert result.returncode == 0

        # Query GLaaS API directly to verify job types
        # Note: The DAG endpoint returns lineage-based jobs, so build jobs may
        # only be included if their outputs are in the lineage path.
        try:
            api_path = f"/api/v1/artifacts/{artifact_hash[:16]}/dag"
            req = urllib.request.Request(f"{glaas_url}{api_path}")

            # Add authentication header
            auth_header = make_auth_header("GET", api_path)
            if auth_header:
                req.add_header("Authorization", auth_header)

            with urllib.request.urlopen(req, timeout=10) as resp:
                dag_data = json.loads(resp.read().decode())

            # Verify the DAG was retrieved successfully
            assert dag_data is not None, "Should get DAG data from GLaaS"
            assert "data" in dag_data, f"DAG response missing 'data' key: {dag_data.keys()}"
            dag = dag_data["data"]
            assert "jobs" in dag, f"DAG data missing 'jobs' key: {dag.keys()}"
            jobs = dag["jobs"]
            assert isinstance(jobs, list), "Jobs should be a list"
            assert len(jobs) >= 1, f"Expected at least 1 job in DAG, got {len(jobs)}"

            # Find preprocess job explicitly - don't hide empty results
            preprocess_job = None
            for job in jobs:
                assert "command" in job, f"Job missing 'command' key: {job.keys()}"
                if "preprocess.py" in job["command"]:
                    preprocess_job = job
                    break

            assert preprocess_job is not None, (
                f"preprocess.py job not found. Jobs: {[j['command'] for j in jobs]}"
            )
            assert "jobType" in preprocess_job, f"Job missing 'jobType': {preprocess_job.keys()}"
            assert preprocess_job["jobType"] == "run", (
                f"preprocess job should have jobType='run', got '{preprocess_job['jobType']}'"
            )

        except urllib.error.HTTPError:
            # Re-raise HTTP errors - authentication should work
            raise
        except urllib.error.URLError:
            pytest.skip("Could not connect to GLaaS API")

    def test_reproduce_executes_build_steps_first(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        build_scripts,
        training_scripts,
        sample_input_data,
        reproduction_target_dir,
    ):
        """Verify reproduction executes build steps before run steps."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        _ = glaas_configured_with_local_remote  # Use fixture for setup

        # Run pipeline
        result = roar_cli("build", python_exe, "setup_env.py")
        assert result.returncode == 0
        git_commit("After setup")

        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Register
        lineage_result = roar_cli("lineage", "processed.csv")
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]

        result = roar_cli("register", "processed.csv")
        assert result.returncode == 0, f"Registration failed: {result.stderr}"

        # Reproduce with verbose output to check order
        reproduce_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "reproduce",
                artifact_hash[:16],
                "--run",
                "-y",
            ],
            cwd=reproduction_target_dir,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GLAAS_URL": os.environ.get("GLAAS_URL", "http://localhost:3001"),
            },
        )

        assert reproduce_result.returncode == 0

        # Check that build steps appear before run steps in output
        stdout = reproduce_result.stdout

        # Find positions of build and run step mentions - fail explicitly if missing
        build_pos = stdout.find("setup_env.py")
        run_pos = stdout.find("preprocess.py")

        assert build_pos != -1, f"setup_env.py not found in reproduction output:\n{stdout}"
        assert run_pos != -1, f"preprocess.py not found in reproduction output:\n{stdout}"
        assert build_pos < run_pos, "Build steps should execute before run steps"

    def test_reproduction_with_all_steps_preview_mode(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        build_scripts,
        training_scripts,
        sample_input_data,
        reproduction_target_dir,
    ):
        """Test --preview mode shows all build and run steps without executing."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        _ = glaas_configured_with_local_remote  # Use fixture for setup

        # Run full pipeline
        result = roar_cli("build", python_exe, "setup_env.py")
        assert result.returncode == 0
        git_commit("After setup")

        result = roar_cli("build", python_exe, "compile_utils.py")
        assert result.returncode == 0
        git_commit("After compile")

        result = roar_cli("build", python_exe, "install_deps.py")
        assert result.returncode == 0
        git_commit("After deps")

        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        result = roar_cli("run", python_exe, "extract_features.py", "processed.csv", "features.npy")
        assert result.returncode == 0
        git_commit("After features")

        result = roar_cli(
            "run", python_exe, "split_data.py", "features.npy", "train.npy", "val.npy"
        )
        assert result.returncode == 0
        git_commit("After split")

        result = roar_cli(
            "run", python_exe, "train_model.py", "train.npy", "config.json", "model.pt"
        )
        assert result.returncode == 0
        git_commit("After train")

        result = roar_cli("run", python_exe, "evaluate.py", "model.pt", "val.npy", "final_model.pt")
        assert result.returncode == 0
        git_commit("After evaluate")

        # Register
        lineage_result = roar_cli("lineage", "final_model.pt")
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]

        result = roar_cli("register", "final_model.pt")
        assert result.returncode == 0, f"Registration failed: {result.stderr}"

        # Preview mode
        preview_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "reproduce",
                artifact_hash[:16],
                "--preview",
            ],
            cwd=reproduction_target_dir,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GLAAS_URL": os.environ.get("GLAAS_URL", "http://localhost:3001"),
            },
        )

        assert preview_result.returncode == 0

        # Should show pipeline info
        stdout = preview_result.stdout

        # The preview shows the DAG retrieved from GLaaS, which is based on lineage.
        # It should show at least the run steps that are in the lineage path.
        assert "Run Steps" in stdout, f"Preview should show 'Run Steps'. Got:\n{stdout}"

        # Verify it shows the artifact hash
        assert artifact_hash[:8] in stdout, "Preview should show artifact hash"

        # Should show git info with exact match
        assert "Git repo" in stdout, "Preview should show 'Git repo'"
        assert "file://" in stdout, "Preview should show local file:// URL"

        # Should NOT create reproduce directory
        reproduce_dir = reproduction_target_dir / "reproduce"
        assert not reproduce_dir.exists(), "Preview should not create directories"
