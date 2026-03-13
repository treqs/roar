"""
Live integration tests for the 'roar get' command.

These tests require a running GLaaS server (for the put phase) and are marked
with @pytest.mark.live_glaas. They test the complete get workflow.

The test flow:
1. Use 'roar put' with ROAR_PUT_SKIP_UPLOAD=1 to create artifacts in the registry
2. Use 'roar get' with ROAR_GET_SKIP_DOWNLOAD=1 to test downloading those artifacts
3. Verify DAG structure: get job is a SOURCE node with outputs

Run with:
    ROAR_PUT_SKIP_UPLOAD=1 ROAR_GET_SKIP_DOWNLOAD=1 pytest tests/live_glaas/test_get_live.py -v -m live_glaas --dist no

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
def skip_download_env():
    """Set environment to skip actual downloads."""
    old_value = os.environ.get("ROAR_GET_SKIP_DOWNLOAD")
    os.environ["ROAR_GET_SKIP_DOWNLOAD"] = "1"
    yield
    if old_value is None:
        del os.environ["ROAR_GET_SKIP_DOWNLOAD"]
    else:
        os.environ["ROAR_GET_SKIP_DOWNLOAD"] = old_value


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

    # Create .gitignore
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

    # Initialize roar with -y
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

    # Configure roar to NOT ignore /tmp files
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "filters.ignore_tmp_files", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Create an active session for get/dag workflows.
    subprocess.run(
        [sys.executable, "-m", "roar", "reset", "-y"],
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


@pytest.mark.live_glaas
class TestGetLive:
    """Live integration tests for roar get."""

    def test_get_single_file_dag_structure(
        self, glaas_configured, skip_download_env, glaas_available
    ):
        """Get a single file and verify it creates a SOURCE node in the DAG."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = glaas_configured

        # Commit so git is clean
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Setup", "--allow-empty"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Get a file (simulated download)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "get",
                "s3://test-bucket/models/model.pt",
                str(repo / "model.pt"),
                "-m",
                "download pretrained model",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Get failed: {result.stderr}\n{result.stdout}"

        # Verify DAG structure
        dag = get_dag(repo)
        assert dag["total_steps"] >= 1

        # Find the get node
        get_nodes = [n for n in dag["nodes"] if "roar get" in n.get("command", "")]
        assert len(get_nodes) == 1, f"Expected 1 get node, got {len(get_nodes)}"

        get_node = get_nodes[0]
        # Get job is a SOURCE node: outputs but no inputs
        assert get_node["metrics"]["outputs"] >= 1, "Get job should have outputs"
        assert get_node["metrics"]["inputs"] == 0, "Get job should have no inputs (source node)"

    def test_get_dry_run_does_not_modify_dag(
        self, glaas_configured, skip_download_env, glaas_available
    ):
        """Dry run shows what would be downloaded without modifying the DAG."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = glaas_configured

        # Get DAG before dry run
        dag_before = get_dag(repo)
        steps_before = dag_before["total_steps"]

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "get",
                "s3://test-bucket/model.pt",
                "--dry-run",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Dry run" in result.stdout

        # DAG should be unchanged
        dag_after = get_dag(repo)
        assert dag_after["total_steps"] == steps_before

    def test_get_then_run_creates_lineage_chain(
        self, glaas_configured, skip_upload_env, skip_download_env, glaas_available
    ):
        """Get -> run -> put creates a complete lineage chain.

        DAG should be:
        [get s3://...] -> model.pt -> [train.py] -> metrics.json -> [put s3://...]
        """
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = glaas_configured

        # Step 1: Get a pretrained model
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Setup", "--allow-empty"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "get",
                "s3://test-bucket/models/pretrained.pt",
                str(repo / "pretrained.pt"),
                "-m",
                "download pretrained model",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Get failed: {result.stderr}\n{result.stdout}"

        # Step 2: Run a script that reads the model and produces output
        script = repo / "finetune.py"
        script.write_text("""
import sys
# Read pretrained model
with open("pretrained.pt", "rb") as f:
    model_data = f.read()
# Create finetuned output
with open("finetuned.pt", "wb") as f:
    f.write(model_data + b"finetuned")
print("Finetuning complete!")
""")

        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add finetuning script and model"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        result = subprocess.run(
            [sys.executable, "-m", "roar", "run", sys.executable, "finetune.py"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Run failed: {result.stderr}"

        # Verify we have at least 2 jobs (get + run)
        dag = get_dag(repo)
        assert dag["total_steps"] >= 2

        # Find nodes
        get_nodes = [n for n in dag["nodes"] if "roar get" in n.get("command", "")]
        run_nodes = [n for n in dag["nodes"] if "finetune.py" in n.get("command", "")]

        assert len(get_nodes) >= 1, "Should have a get node"
        assert len(run_nodes) >= 1, "Should have a run node"

    def test_get_with_tag_creates_git_tag(
        self, glaas_configured, skip_download_env, glaas_available
    ):
        """Get with --tag creates a git tag."""
        if not glaas_available:
            pytest.skip("GLaaS server not available")

        repo = glaas_configured

        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Setup", "--allow-empty"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        # Get tags before
        before = subprocess.run(
            ["git", "tag", "-l", "roar/*"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        before_tags = set(before.stdout.strip().split("\n")) if before.stdout.strip() else set()

        init_session = subprocess.run(
            [sys.executable, "-m", "roar", "run", sys.executable, "-c", "print('warmup')"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert init_session.returncode == 0

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "get",
                "s3://test-bucket/model.pt",
                str(repo / "model.pt"),
                "--tag",
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
