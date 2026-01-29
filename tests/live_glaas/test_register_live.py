"""
Live integration tests for the 'roar register' command.

These tests require a running GLaaS server and are marked with @pytest.mark.live_glaas.
They test the complete registration workflow including API communication.

Run with:
    pytest tests/live_glaas/test_register_live.py -v -m live_glaas --dist no

Prerequisites:
    1. Start glaas-api: cd /path/to/glaas-api && npm run dev
    2. Ensure GLAAS_URL env var is set or server is at http://localhost:3001
"""

import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest


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
def glaas_configured(temp_git_repo, glaas_url):
    """Configure GLaaS URL for test repo and add fake git remote."""
    # Add a fake git remote (required for registration validation)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    # Configure GLaaS URL
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "glaas.url", glaas_url],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    return temp_git_repo


@pytest.fixture
def python_exe() -> str:
    """Return the absolute path to the Python executable."""
    return sys.executable


@pytest.fixture
def sample_scripts(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """Create sample Python scripts for testing."""
    scripts = {}

    # Preprocess script
    preprocess = temp_git_repo / "preprocess.py"
    preprocess.write_text("""
import sys

input_file = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "processed.csv"

with open(input_file, "r") as f:
    data = f.read()

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

with open(input_file, "r") as f:
    data = f.read()

model = {"data_hash": hash(data), "trained": True}

with open(output_file, "w") as f:
    json.dump(model, f)

print(f"Trained model from {input_file} -> {output_file}")
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

metrics = {"accuracy": 0.95, "loss": 0.05}

with open(output_file, "w") as f:
    json.dump(metrics, f, indent=2)

print(f"Evaluated {model_file} on {test_file} -> {output_file}")
""")
    scripts["evaluate"] = evaluate

    git_commit("Add sample scripts")
    return scripts


@pytest.fixture
def sample_data(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """Create sample input data files."""
    data_files = {}

    input_csv = temp_git_repo / "input.csv"
    input_csv.write_text("id,value\n1,foo\n2,bar\n3,baz\n")
    data_files["input"] = input_csv

    test_csv = temp_git_repo / "test.csv"
    test_csv.write_text("id,value\n4,qux\n5,quux\n")
    data_files["test"] = test_csv

    git_commit("Add sample data")
    return data_files


@pytest.mark.live_glaas
class TestRegisterLiveGlaas:
    """Live integration tests for roar register command."""

    def test_register_simple_artifact(
        self,
        glaas_configured,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        sample_scripts,
        sample_data,
    ):
        """Test registering a single tracked artifact."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        # Run a tracked command to create an artifact
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Register the artifact
        result = roar_cli("register", "processed.csv")
        assert result.returncode == 0
        assert "Registered lineage" in result.stdout or "registered" in result.stdout.lower()

    def test_register_pipeline_lineage(
        self,
        glaas_configured,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        sample_scripts,
        sample_data,
    ):
        """Test registering artifact from a multi-step pipeline."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        # Step 1: Preprocess
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Step 2: Train
        result = roar_cli("run", python_exe, "train.py", "processed.csv", "model.pkl")
        assert result.returncode == 0
        git_commit("After train")

        # Step 3: Evaluate
        result = roar_cli("run", python_exe, "evaluate.py", "model.pkl", "test.csv", "metrics.json")
        assert result.returncode == 0
        git_commit("After evaluate")

        # Register the final artifact - should include entire lineage
        result = roar_cli("register", "metrics.json")
        assert result.returncode == 0
        # Should report multiple jobs in the lineage
        output = result.stdout.lower()
        assert "job" in output or "registered" in output

    def test_register_verifiable_in_glaas(
        self,
        glaas_configured,
        glaas_available,
        glaas_url,
        roar_cli,
        git_commit,
        python_exe,
        sample_scripts,
        sample_data,
    ):
        """Test that registered artifact produces verifiable output."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        # Run and register
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Get the hash of the artifact
        lineage_result = roar_cli("lineage", "processed.csv")
        assert lineage_result.returncode == 0
        lineage = json.loads(lineage_result.stdout)
        _ = lineage["artifact"]["hash"]  # Verify hash exists

        # Register
        result = roar_cli("register", "processed.csv")
        assert result.returncode == 0

        # Verify registration output includes session hash and counts
        output = result.stdout
        assert "Session:" in output
        assert "Jobs:" in output
        assert "Artifacts:" in output

        # Verify the session hash prefix is in output
        # (The session is derived from the roar_dir and session_id)
        assert "..." in output  # Session hash is truncated with ...

        # Verify at least 1 job was registered
        # Note: Artifact count depends on GLaaS batch API behavior
        lines = output.strip().split("\n")
        for line in lines:
            if "Jobs:" in line:
                job_count = int(line.split(":")[1].strip())
                assert job_count >= 1, "Expected at least 1 job registered"

    def test_register_idempotent(
        self,
        glaas_configured,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        sample_scripts,
        sample_data,
    ):
        """Test that re-registering the same artifact is safe (idempotent)."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        # Run a tracked command
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Register first time
        result1 = roar_cli("register", "processed.csv")
        assert result1.returncode == 0

        # Register second time - should succeed without error
        result2 = roar_cli("register", "processed.csv")
        assert result2.returncode == 0

    def test_register_dry_run(
        self,
        glaas_configured,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        sample_scripts,
        sample_data,
    ):
        """Test dry-run mode shows what would be registered."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        # Run a tracked command
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Dry run should show counts without actually registering
        result = roar_cli("register", "--dry-run", "processed.csv")
        assert result.returncode == 0
        output = result.stdout.lower()
        # Should indicate it's a dry run
        assert "dry" in output or "would" in output or "preview" in output
