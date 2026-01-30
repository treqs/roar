"""
Live integration tests for the 'roar reproduce' command.

These tests require a running GLaaS server and are marked with @pytest.mark.live_glaas.
They test the complete reproduction workflow including API communication.

Run with:
    pytest tests/live_glaas/test_reproduce_live.py -v -m live_glaas --dist no

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
def sample_pipeline_scripts(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """Create preprocess.py and train.py that produce deterministic model.pt.

    Scripts produce deterministic output based on input content hash.
    """
    scripts = {}

    # Preprocess script - reads input.csv, writes processed.csv
    preprocess = temp_git_repo / "preprocess.py"
    preprocess.write_text("""
import sys
import hashlib

input_file = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "processed.csv"

with open(input_file, "r") as f:
    data = f.read()

# Deterministic transformation
processed = data.upper()

with open(output_file, "w") as f:
    f.write(processed)

print(f"Processed {input_file} -> {output_file}")
""")
    scripts["preprocess"] = preprocess

    # Train script - reads processed.csv, writes model.pt
    # Produces deterministic output: model content = hash of input
    train = temp_git_repo / "train.py"
    train.write_text("""
import sys
import hashlib

input_file = sys.argv[1] if len(sys.argv) > 1 else "processed.csv"
output_file = sys.argv[2] if len(sys.argv) > 2 else "model.pt"

with open(input_file, "r") as f:
    data = f.read()

# Deterministic model: use input hash as "weights"
# This guarantees identical output for identical inputs
input_hash = hashlib.sha256(data.encode()).hexdigest()
model_content = f"MODEL_WEIGHTS:{input_hash}\\n"

with open(output_file, "w") as f:
    f.write(model_content)

print(f"Trained model from {input_file} -> {output_file}")
""")
    scripts["train"] = train

    git_commit("Add pipeline scripts")
    return scripts


@pytest.fixture
def sample_input_data(temp_git_repo: Path, git_commit: Callable) -> dict[str, Path]:
    """Create sample input data file."""
    data_files = {}

    input_csv = temp_git_repo / "input.csv"
    input_csv.write_text("id,value\n1,foo\n2,bar\n3,baz\n")
    data_files["input"] = input_csv

    git_commit("Add input data")
    return data_files


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


@pytest.mark.live_glaas
class TestReproduceLiveGlaas:
    """Live integration tests for roar reproduce command."""

    def test_reproduce_full_pipeline(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        sample_pipeline_scripts,
        sample_input_data,
        reproduction_target_dir,
        compute_file_hash,
    ):
        """Test full end-to-end reproduction: run, register, reproduce, verify hash."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        source_repo = glaas_configured_with_local_remote

        # Step 1: Run preprocess
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        # Step 2: Run train
        result = roar_cli("run", python_exe, "train.py", "processed.csv", "model.pt")
        assert result.returncode == 0
        git_commit("After train")

        # Compute original model.pt hash
        original_model = source_repo / "model.pt"
        assert original_model.exists()
        original_hash = compute_file_hash(original_model)

        # Step 3: Get artifact hash for model.pt
        lineage_result = roar_cli("lineage", "model.pt")
        assert lineage_result.returncode == 0
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]

        # Step 4: Register with GLaaS
        result = roar_cli("register", "model.pt")
        assert result.returncode == 0
        assert "Session:" in result.stdout or "registered" in result.stdout.lower()

        # Step 5: Reproduce in fresh directory with --run -y
        # We need to run reproduce from a different directory
        reproduce_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "reproduce",
                artifact_hash[:16],  # Use 16-char prefix
                "--run",
                "-y",
            ],
            cwd=reproduction_target_dir,
            capture_output=True,
            text=True,
            env={**os.environ, "GLAAS_URL": os.environ.get("GLAAS_URL", "http://localhost:3001")},
        )

        # Check reproduction succeeded
        assert reproduce_result.returncode == 0, (
            f"Reproduce failed: {reproduce_result.stderr}\n{reproduce_result.stdout}"
        )
        assert "Reproduction Complete" in reproduce_result.stdout

        # Step 6: Verify reproduced model.pt matches original
        # The reproduce directory structure is: <target>/reproduce/<repo_name>/model.pt
        reproduce_dir = reproduction_target_dir / "reproduce"
        if reproduce_dir.exists():
            # Find the model.pt in any subdirectory
            model_files = list(reproduce_dir.rglob("model.pt"))
            assert len(model_files) >= 1, f"No model.pt found in {reproduce_dir}"

            reproduced_model = model_files[0]
            reproduced_hash = compute_file_hash(reproduced_model)

            assert original_hash == reproduced_hash, (
                f"Hash mismatch: original={original_hash[:16]}... "
                f"reproduced={reproduced_hash[:16]}..."
            )

    def test_reproduce_default_shows_preview(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        roar_cli,
        git_commit,
        python_exe,
        sample_pipeline_scripts,
        sample_input_data,
        reproduction_target_dir,
    ):
        """Test default behavior (no --run): shows preview with copy-paste command, no setup."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        _ = glaas_configured_with_local_remote  # Use fixture for setup

        # Run pipeline
        result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
        assert result.returncode == 0
        git_commit("After preprocess")

        result = roar_cli("run", python_exe, "train.py", "processed.csv", "model.pt")
        assert result.returncode == 0
        git_commit("After train")

        # Get artifact hash
        lineage_result = roar_cli("lineage", "model.pt")
        lineage = json.loads(lineage_result.stdout)
        artifact_hash = lineage["artifact"]["hash"]

        # Register
        result = roar_cli("register", "model.pt")
        assert result.returncode == 0

        # Reproduce without --run (should show preview only)
        reproduce_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "reproduce",
                artifact_hash[:16],
            ],
            cwd=reproduction_target_dir,
            capture_output=True,
            text=True,
            env={**os.environ, "GLAAS_URL": os.environ.get("GLAAS_URL", "http://localhost:3001")},
        )

        assert reproduce_result.returncode == 0, f"Reproduce failed: {reproduce_result.stderr}"

        # Should show artifact info
        assert (
            "Artifact:" in reproduce_result.stdout or artifact_hash[:8] in reproduce_result.stdout
        )

        # Should show git info
        output = reproduce_result.stdout.lower()
        assert "git" in output or "repo" in output

        # Should show copy-paste command for --run
        assert "--run" in reproduce_result.stdout
        assert "roar reproduce" in reproduce_result.stdout

        # Should NOT create reproduce directory (preview only)
        reproduce_dir = reproduction_target_dir / "reproduce"
        assert not reproduce_dir.exists(), "Preview should not create directories"

        # Should NOT show "Reproduction Complete"
        assert "Reproduction Complete" not in reproduce_result.stdout

    def test_reproduce_artifact_not_found(
        self,
        glaas_configured_with_local_remote,
        glaas_available,
        reproduction_target_dir,
    ):
        """Test graceful failure for unknown artifact hash."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        # Use a fake hash that won't exist
        fake_hash = "deadbeef" * 8  # 64 chars

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "reproduce",
                fake_hash[:16],
                "-y",
            ],
            cwd=reproduction_target_dir,
            capture_output=True,
            text=True,
            env={**os.environ, "GLAAS_URL": os.environ.get("GLAAS_URL", "http://localhost:3001")},
        )

        # Should fail gracefully
        assert result.returncode != 0

        # Should have error message
        output = result.stdout + result.stderr
        assert "not found" in output.lower() or "error" in output.lower()
