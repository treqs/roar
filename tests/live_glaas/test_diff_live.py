"""Live diff contract tests against the pm2-managed GLaaS API.

These tests validate the real DAG payload shape that ``roar diff`` must consume.
They intentionally target the locally running ``glaas-api`` process (default:
``http://localhost:3001``) instead of the ephemeral live-test server used by some
other live suites.

Run with:
    .venv/bin/pytest tests/live_glaas/test_diff_live.py -m live_glaas --dist no
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest

from roar.integrations.glaas import GlaasClient

pytestmark = [pytest.mark.live_glaas, pytest.mark.e2e]


@pytest.fixture
def pm2_glaas_url() -> str:
    """Return the URL for the locally running pm2-managed GLaaS API."""
    return os.environ.get("GLAAS_URL", "http://localhost:3001")


@pytest.fixture
def pm2_glaas_available(pm2_glaas_url: str) -> bool:
    """Check whether the pm2-managed GLaaS API is reachable."""
    try:
        req = urllib.request.Request(f"{pm2_glaas_url}/api/v1/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture
def pm2_glaas_client(pm2_glaas_url: str) -> GlaasClient:
    """Create a GLaaS client pointed at the pm2-managed API."""
    return GlaasClient(pm2_glaas_url)


@pytest.fixture
def glaas_configured_repo(temp_git_repo: Path, pm2_glaas_url: str) -> Path:
    """Configure a temp repo to talk to the pm2-managed GLaaS API."""
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/diff-live.git"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "glaas.url", pm2_glaas_url],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    return temp_git_repo


@pytest.fixture
def python_exe() -> str:
    """Return the current Python executable."""
    return sys.executable


def _artifact_hash(roar_cli, target: str) -> str:
    result = roar_cli("lineage", target)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    return data["artifact"]["hash"]


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _git_commit40(label: str) -> str:
    return _hex64(label)[:40]


def _assert_no_error(error: str | None) -> None:
    assert error is None, error


@pytest.mark.timeout(120)
def test_registered_artifact_dag_uses_real_pm2_contract(
    glaas_configured_repo: Path,
    pm2_glaas_available: bool,
    pm2_glaas_client: GlaasClient,
    roar_cli,
    git_commit,
    python_exe: str,
) -> None:
    """A product-path registration should expose the real camelCase DAG payload.

    This is the payload shape that ``roar diff glaas:<hash>`` must consume.
    """
    if not pm2_glaas_available:
        pytest.skip("pm2-managed GLaaS API not available")

    repo = glaas_configured_repo
    token = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]

    (repo / "preprocess.py").write_text(
        """
import sys

input_file = sys.argv[1]
output_file = sys.argv[2]

with open(input_file, "r", encoding="utf-8") as handle:
    data = handle.read()

with open(output_file, "w", encoding="utf-8") as handle:
    handle.write(data.upper())
""".strip()
        + "\n"
    )
    (repo / "input.csv").write_text(f"id,value\n1,{token}\n", encoding="utf-8")
    git_commit("Add diff live DAG fixture")

    run_result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
    assert run_result.returncode == 0, run_result.stderr
    git_commit("Track processed output for DAG contract test")

    artifact_hash = _artifact_hash(roar_cli, "processed.csv")

    register_result = roar_cli("register", "processed.csv")
    assert register_result.returncode == 0, register_result.stderr

    dag, error = pm2_glaas_client.get_artifact_dag(artifact_hash[:16])
    _assert_no_error(error)
    assert dag is not None
    assert dag["isExternal"] is False
    assert isinstance(dag["gitRepo"], str) and dag["gitRepo"]
    assert isinstance(dag["gitCommit"], str) and dag["gitCommit"]
    assert isinstance(dag["externalDeps"], list)
    assert isinstance(dag["jobs"], list) and dag["jobs"]

    preprocess_job = next(job for job in dag["jobs"] if "preprocess.py" in job["command"])
    expected_job_keys = {
        "id",
        "jobUid",
        "command",
        "jobType",
        "stepNumber",
        "timestamp",
        "metadata",
        "inputs",
        "outputs",
        "children",
    }
    assert expected_job_keys.issubset(preprocess_job.keys())
    assert "job_uid" not in preprocess_job
    assert "step_number" not in preprocess_job
    assert isinstance(preprocess_job["jobUid"], str) and preprocess_job["jobUid"]
    assert isinstance(preprocess_job["stepNumber"], int)
    assert isinstance(preprocess_job["children"], list)
    assert preprocess_job["jobType"] == "run"

    assert preprocess_job["inputs"]
    assert preprocess_job["outputs"]
    for io_entry in [*preprocess_job["inputs"], *preprocess_job["outputs"]]:
        assert {"hash", "path"}.issubset(io_entry.keys())
        assert isinstance(io_entry["hash"], str) and io_entry["hash"]
        assert isinstance(io_entry["path"], str) and io_entry["path"]


@pytest.mark.timeout(120)
def test_roar_diff_can_compare_local_artifact_to_glaas_reference(
    glaas_configured_repo: Path,
    pm2_glaas_available: bool,
    roar_cli,
    git_commit,
    python_exe: str,
) -> None:
    """The product-path ``roar diff`` command should consume the real GLaaS DAG shape."""
    if not pm2_glaas_available:
        pytest.skip("pm2-managed GLaaS API not available")

    repo = glaas_configured_repo
    token = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]

    (repo / "preprocess.py").write_text(
        """
import sys

input_file = sys.argv[1]
output_file = sys.argv[2]

with open(input_file, "r", encoding="utf-8") as handle:
    data = handle.read()

with open(output_file, "w", encoding="utf-8") as handle:
    handle.write(data.upper())
""".strip()
        + "\n"
    )
    (repo / "input.csv").write_text(f"id,value\n1,{token}\n", encoding="utf-8")
    git_commit("Add diff live CLI fixture")

    run_result = roar_cli("run", python_exe, "preprocess.py", "input.csv", "processed.csv")
    assert run_result.returncode == 0, run_result.stderr
    git_commit("Track processed output for diff CLI live test")

    artifact_hash = _artifact_hash(roar_cli, "processed.csv")

    register_result = roar_cli("register", "processed.csv")
    assert register_result.returncode == 0, register_result.stderr

    diff_result = roar_cli("diff", "processed.csv", f"glaas:{artifact_hash[:16]}")
    assert diff_result.returncode == 0, diff_result.stderr
    output = diff_result.stdout.lower()
    assert "no differences found in lineage" in output or "identical" in output


@pytest.mark.timeout(120)
def test_registered_artifact_dag_preserves_nested_child_jobs(
    pm2_glaas_available: bool,
    pm2_glaas_client: GlaasClient,
) -> None:
    """The pm2-managed DAG endpoint should return nested child jobs under ``children``."""
    if not pm2_glaas_available:
        pytest.skip("pm2-managed GLaaS API not available")

    unique = uuid4().hex
    session_hash = _hex64(f"diff-live-session:{unique}")
    git_commit = _git_commit40(f"diff-live-commit:{unique}")
    parent_job_uid = f"parent-{unique}"
    child_job_uid = f"child-{unique}"
    raw_hash = _hex64(f"raw:{unique}")
    checkpoint_hash = _hex64(f"checkpoint:{unique}")
    final_hash = _hex64(f"final:{unique}")
    base_timestamp = time.time()

    _session, error = pm2_glaas_client.register_session(
        session_hash,
        f"https://github.com/test/diff-live-{unique}.git",
        git_commit,
        "main",
    )
    _assert_no_error(error)

    for digest, source_url in (
        (raw_hash, f"https://example.com/{unique}/raw.csv"),
        (checkpoint_hash, f"https://example.com/{unique}/checkpoint.pt"),
        (final_hash, f"https://example.com/{unique}/model.pt"),
    ):
        success, artifact_error = pm2_glaas_client.register_artifact(
            hashes=[{"algorithm": "blake3", "digest": digest}],
            size=128,
            source_type="https",
            session_hash=session_hash,
            source_url=source_url,
        )
        assert success, artifact_error

    _parent_id, error = pm2_glaas_client.register_job(
        session_hash=session_hash,
        command="python train.py --epochs 2",
        timestamp=base_timestamp + 1,
        job_uid=parent_job_uid,
        git_commit=git_commit,
        git_branch="main",
        duration_seconds=2.0,
        exit_code=0,
        job_type="run",
        step_number=2,
        metadata=json.dumps({"role": "driver"}),
    )
    _assert_no_error(error)

    _child_id, error = pm2_glaas_client.register_job(
        session_hash=session_hash,
        command="ray_task:train_shard",
        timestamp=base_timestamp,
        job_uid=child_job_uid,
        git_commit=git_commit,
        git_branch="main",
        duration_seconds=1.5,
        exit_code=0,
        job_type="ray_task",
        step_number=1,
        metadata=json.dumps({"worker": 1}),
        parent_job_uid=parent_job_uid,
    )
    _assert_no_error(error)

    _child_inputs, error = pm2_glaas_client.register_job_inputs(
        session_hash,
        child_job_uid,
        [{"artifact_hash": raw_hash, "path": "/input/raw.csv"}],
    )
    _assert_no_error(error)
    _child_outputs, error = pm2_glaas_client.register_job_outputs(
        session_hash,
        child_job_uid,
        [{"artifact_hash": checkpoint_hash, "path": "/output/checkpoint.pt"}],
    )
    _assert_no_error(error)

    _parent_inputs, error = pm2_glaas_client.register_job_inputs(
        session_hash,
        parent_job_uid,
        [{"artifact_hash": checkpoint_hash, "path": "/input/checkpoint.pt"}],
    )
    _assert_no_error(error)
    _parent_outputs, error = pm2_glaas_client.register_job_outputs(
        session_hash,
        parent_job_uid,
        [{"artifact_hash": final_hash, "path": "/output/model.pt"}],
    )
    _assert_no_error(error)

    dag, error = pm2_glaas_client.get_artifact_dag(final_hash[:16])
    _assert_no_error(error)
    assert dag is not None

    parent_job = next(job for job in dag["jobs"] if job["jobUid"] == parent_job_uid)
    assert parent_job["stepNumber"] == 2
    assert parent_job["jobType"] == "run"
    assert isinstance(parent_job["children"], list) and parent_job["children"]

    child_job = next(job for job in parent_job["children"] if job["jobUid"] == child_job_uid)
    expected_child_keys = {
        "id",
        "jobUid",
        "command",
        "jobType",
        "stepNumber",
        "timestamp",
        "metadata",
        "inputs",
        "outputs",
        "children",
    }
    assert expected_child_keys.issubset(child_job.keys())
    assert "job_uid" not in child_job
    assert "step_number" not in child_job
    assert child_job["jobType"] == "ray_task"
    assert child_job["stepNumber"] == 1
    assert child_job["children"] == []
    assert any(item["hash"] == raw_hash for item in child_job["inputs"])
    assert any(item["hash"] == checkpoint_hash for item in child_job["outputs"])
