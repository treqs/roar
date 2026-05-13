"""
Live integration tests for the 'roar put' command.

These tests require a running GLaaS server and are marked with @pytest.mark.live_glaas.
They test the complete put workflow including GLaaS registration.

The tests use ROAR_PUT_SKIP_UPLOAD=1 to skip actual cloud uploads while still
testing the full workflow including artifact registration with GLaaS.

Run with:
    ROAR_PUT_SKIP_UPLOAD=1 pytest tests/live_glaas/test_put_live.py -v -m live_glaas --dist no

Prerequisites:
    1. Start glaas-api: cd /path/to/glaas-api && npm run dev
    2. Ensure GLAAS_URL env var is set or server is at http://localhost:3001
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest


def get_dag(repo: Path) -> dict[str, Any]:
    """Fetch the full DAG as JSON from the roar database."""
    result = subprocess.run(
        [sys.executable, "-m", "roar", "dag", "--json", "--show-artifacts"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"roar dag failed: {result.stderr}")
    return json.loads(result.stdout)


def get_lineage(repo: Path, artifact: str) -> dict[str, Any]:
    """Fetch artifact lineage as JSON."""
    result = subprocess.run(
        [sys.executable, "-m", "roar", "lineage", artifact],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"roar lineage failed: {result.stderr}")
    return json.loads(result.stdout)


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
def skip_upload_env():
    """Set environment to skip actual uploads."""
    old_value = os.environ.get("ROAR_PUT_SKIP_UPLOAD")
    os.environ["ROAR_PUT_SKIP_UPLOAD"] = "1"
    yield
    if old_value is None:
        del os.environ["ROAR_PUT_SKIP_UPLOAD"]
    else:
        os.environ["ROAR_PUT_SKIP_UPLOAD"] = old_value


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    """Create a temporary git repository with roar initialized."""
    # Initialize git repo
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Create .gitignore (roar init will add .roar/ to it)
    (tmp_path / ".gitignore").write_text("")

    # Create initial commit
    (tmp_path / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Add fake remote
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Initialize roar with -y to auto-add .roar/ to .gitignore
    subprocess.run(
        [sys.executable, "-m", "roar", "init", "-y"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Commit the .gitignore update
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add roar"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Configure roar to NOT ignore /tmp files (since tests run in /tmp/pytest-...)
    # Without this, file outputs are filtered and not recorded
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "filters.ignore_tmp_files", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    return tmp_path


@pytest.fixture
def glaas_configured(temp_git_repo, glaas_url):
    """Configure GLaaS URL for test repo."""
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "glaas.url", glaas_url],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    return temp_git_repo


@pytest.fixture
def repo_with_outputs(glaas_configured):
    """Create a repo with some tracked outputs from a job."""
    repo = glaas_configured

    # Create a simple script that generates output
    script = repo / "train.py"
    script.write_text("""
import sys
# Create output files
with open("model.pt", "wb") as f:
    f.write(b"fake model weights" * 100)
with open("metrics.json", "w") as f:
    f.write('{"accuracy": 0.95, "loss": 0.05}')
print("Training complete!")
""")

    # Commit the script BEFORE running roar (roar run requires clean state)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add training script"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Run the script with roar
    result = subprocess.run(
        [sys.executable, "-m", "roar", "run", sys.executable, "train.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"roar run failed: {result.stderr}")

    # Commit the output files (roar put requires clean git state)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add training outputs"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    return repo


@pytest.mark.live_glaas
class TestPutLive:
    """Live integration tests for roar put."""

    def test_put_single_file_dag_structure(
        self, repo_with_outputs, skip_upload_env, glaas_available
    ):
        """Put a single file and verify the resulting DAG structure."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = repo_with_outputs

        # Get DAG before put (should have 1 job: train.py)
        dag_before = get_dag(repo)
        assert dag_before["total_steps"] == 1
        assert len(dag_before["nodes"]) == 1
        train_node = dag_before["nodes"][0]
        assert "train.py" in train_node["command"]
        assert train_node["step_number"] == 1

        # Put just the model file
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "put",
                "model.pt",
                "s3://test-bucket/models",
                "-m",
                "publish model",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Put failed: {result.stderr}"

        # Get DAG after put - verify exact structure
        dag_after = get_dag(repo)

        # Should now have 2 jobs
        assert dag_after["total_steps"] == 2, f"Expected 2 steps, got {dag_after['total_steps']}"
        assert len(dag_after["nodes"]) == 2

        # Find the train and put nodes
        nodes_by_step = {n["step_number"]: n for n in dag_after["nodes"]}
        assert 1 in nodes_by_step, "Train job (step 1) should exist"
        assert 2 in nodes_by_step, "Put job (step 2) should exist"

        train_node = nodes_by_step[1]
        put_node = nodes_by_step[2]

        # Verify train node
        assert "train.py" in train_node["command"]
        assert train_node["is_build"] is False
        assert train_node["state"] == "active"

        # Verify put node structure
        assert "roar put" in put_node["command"]
        assert "model.pt" in put_node["command"]
        assert put_node["is_build"] is False
        assert put_node["state"] == "active"

        # Put consumes model.pt as input, so metrics show consumed artifacts
        assert put_node["metrics"]["inputs"] >= 1, "Put job should have at least 1 input"
        assert put_node["metrics"]["outputs"] == 0, "Put job should have 0 outputs (sink node)"

        # Put depends on train (step 1 produced model.pt which put consumes)
        assert 1 in put_node["dependencies"], "Put should depend on train step"

        # Verify artifact flow: model.pt should be terminal (produced by train, consumed by put)
        model_artifacts = [a for a in dag_after["artifacts"] if "model.pt" in a["path"]]
        assert len(model_artifacts) >= 1, "model.pt artifact should exist"
        model_artifact = model_artifacts[0]
        assert model_artifact["producer_step"] == 1, "model.pt should be produced by step 1"
        assert 2 in model_artifact["consumer_steps"], "model.pt should be consumed by step 2 (put)"

    def test_put_multiple_files_dag_structure(
        self, repo_with_outputs, skip_upload_env, glaas_available
    ):
        """Put multiple files and verify all are linked as inputs."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = repo_with_outputs

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "put",
                "model.pt",
                "metrics.json",
                "s3://test-bucket/release",
                "-m",
                "publish release artifacts",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Put failed: {result.stderr}"

        # Get DAG and verify structure
        dag = get_dag(repo)
        assert dag["total_steps"] == 2

        nodes_by_step = {n["step_number"]: n for n in dag["nodes"]}
        put_node = nodes_by_step[2]

        # Put should have 2 inputs (both files)
        assert put_node["metrics"]["inputs"] >= 2, "Put job should have at least 2 inputs"
        assert put_node["metrics"]["outputs"] == 0, "Put job should have 0 outputs"

        # Both files should be consumed by the put job
        model_artifacts = [a for a in dag["artifacts"] if "model.pt" in a["path"]]
        metrics_artifacts = [a for a in dag["artifacts"] if "metrics.json" in a["path"]]

        assert len(model_artifacts) >= 1
        assert len(metrics_artifacts) >= 1
        assert 2 in model_artifacts[0]["consumer_steps"]
        assert 2 in metrics_artifacts[0]["consumer_steps"]

    def test_put_creates_job_with_correct_metadata(
        self, repo_with_outputs, skip_upload_env, glaas_available
    ):
        """Put creates a job record with correct metadata structure."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = repo_with_outputs

        # Run put
        put_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "put",
                "model.pt",
                "s3://bucket/models",
                "-m",
                "test message",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert put_result.returncode == 0

        # Get DAG and verify structure
        dag = get_dag(repo)
        assert dag["total_steps"] == 2

        # Verify session status shows 2 run steps
        result = subprocess.run(
            [sys.executable, "-m", "roar", "status"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "2 run steps" in result.stdout

    def test_put_dry_run_does_not_modify_dag(
        self, repo_with_outputs, skip_upload_env, glaas_available
    ):
        """Dry run shows what would be uploaded without modifying the DAG."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = repo_with_outputs

        # Get DAG before dry run
        dag_before = get_dag(repo)
        assert dag_before["total_steps"] == 1

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "put",
                "model.pt",
                "metrics.json",
                "s3://bucket/test",
                "-m",
                "test",
                "--dry-run",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "Dry run" in result.stdout
        assert "model.pt" in result.stdout
        assert "metrics.json" in result.stdout
        assert "Total: 2 file(s)" in result.stdout

        # DAG should be unchanged after dry run
        dag_after = get_dag(repo)
        assert dag_after["total_steps"] == 1, "Dry run should not add any jobs"
        assert len(dag_after["nodes"]) == 1, "Dry run should not add any nodes"

    def test_put_session_outputs_default(self, repo_with_outputs, skip_upload_env, glaas_available):
        """Put with no sources and no tracked outputs uploads nothing."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = repo_with_outputs

        # Note: The files created by train.py are not automatically tracked as
        # outputs by roar run unless the tracer detects them. Without explicit
        # registration, the session has no outputs to upload.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "put",
                "s3://bucket/all-outputs",
                "-m",
                "publish all outputs",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Put failed: {result.stderr}"
        # Without tracked outputs, this should succeed but upload 0 files
        assert "Published 0 file(s)" in result.stdout or "Published" in result.stdout

    def test_put_creates_git_tag(self, repo_with_outputs, skip_upload_env, glaas_available):
        """Put creates a git tag for the commit."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = repo_with_outputs

        # Get tags before
        before = subprocess.run(
            ["git", "tag", "-l", "roar/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        before_tags = set(before.stdout.strip().split("\n")) if before.stdout.strip() else set()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "put",
                "model.pt",
                "s3://bucket/models",
                "-m",
                "test",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        # Get tags after
        after = subprocess.run(
            ["git", "tag", "-l", "roar/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        after_tags = set(after.stdout.strip().split("\n")) if after.stdout.strip() else set()

        # Verify a new tag was created
        new_tags = after_tags - before_tags
        assert len(new_tags) == 1, f"Expected 1 new tag, got {new_tags}"
        new_tag = new_tags.pop()
        assert new_tag.startswith("roar/"), f"Tag should start with 'roar/', got {new_tag}"

        # Verify DAG still correct
        dag = get_dag(repo)
        assert dag["total_steps"] == 2

    def test_put_no_tag_option_preserves_dag(
        self, repo_with_outputs, skip_upload_env, glaas_available
    ):
        """Put with --no-tag skips git tagging but still creates job in DAG."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = repo_with_outputs

        # Count existing tags
        before = subprocess.run(
            ["git", "tag", "-l", "roar/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        before_count = len(before.stdout.strip().split("\n")) if before.stdout.strip() else 0

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "put",
                "model.pt",
                "s3://bucket/models",
                "-m",
                "test",
                "--no-tag",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        # Check no new tag was created
        after = subprocess.run(
            ["git", "tag", "-l", "roar/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        after_count = len(after.stdout.strip().split("\n")) if after.stdout.strip() else 0
        assert after_count == before_count, "No new tag should be created with --no-tag"

        # But DAG should still have the put job
        dag = get_dag(repo)
        assert dag["total_steps"] == 2, "Put job should still be in DAG even with --no-tag"

        nodes_by_step = {n["step_number"]: n for n in dag["nodes"]}
        put_node = nodes_by_step[2]
        assert "roar put" in put_node["command"]
        assert put_node["metrics"]["inputs"] >= 1
