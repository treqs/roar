"""Live integration coverage for GLaaS registration of Ray jobs."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_glaas


@pytest.fixture
def glaas_url() -> str:
    """Get GLaaS server URL from environment or default."""
    return os.environ.get("GLAAS_URL", "http://localhost:3001")


@pytest.fixture
def glaas_available(glaas_url: str) -> bool:
    """Check if GLaaS server is available."""
    try:
        req = urllib.request.Request(f"{glaas_url}/api/v1/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture
def glaas_configured(temp_git_repo: Path, glaas_url: str) -> Path:
    """Configure GLaaS URL for the test repo and add fake git remote."""
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
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
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "filters.ignore_tmp_files", "false"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )
    return temp_git_repo


@pytest.fixture(scope="module")
def ray_cluster_available() -> bool:
    """Check if Ray cluster is reachable on port 10001."""
    try:
        conn = socket.create_connection(("localhost", 10001), timeout=3)
        conn.close()
        return True
    except OSError:
        return False


@pytest.fixture
def glaas_client(glaas_url: str):
    from roar.glaas_client import GlaasClient

    return GlaasClient(glaas_url)


@pytest.fixture
def ray_script(temp_git_repo: Path, git_commit) -> Path:
    """Write a Ray script with one driver and three remote tasks."""
    script_path = temp_git_repo / "ray_train.py"
    script_path.write_text(
        """
import json
import os
import uuid
import ray

@ray.remote
def process_shard(shard_id: int, data: str) -> dict:
    out = f"ckpt_{shard_id}.json"
    payload = {"shard": shard_id, "processed": data.upper()}
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return {"checkpoint": os.path.abspath(out), "payload": payload}

ray.init("ray://localhost:10001")
shards = ["alpha data", "beta data", "gamma data"]
results = ray.get([process_shard.remote(i, s) for i, s in enumerate(shards)])

combined = {str(r["payload"]["shard"]): r["payload"]["processed"] for r in results}
run_id = uuid.uuid4().hex
with open("model.json", "w", encoding="utf-8") as f:
    json.dump({"model": "combined", "run_id": run_id, "data": combined}, f)

print("Checkpoints:", [r["checkpoint"] for r in results])
print("Model: model.json")
ray.shutdown()
""".lstrip()
    )
    git_commit("Add ray training script")
    return script_path


def _parse_session_hash(output: str) -> str | None:
    """Extract session hash from roar register output."""
    url_match = re.search(r"/dag/([a-f0-9]{64})", output)
    if url_match:
        return url_match.group(1)
    session_match = re.search(r"[Ss]ession(?:\s+[Hh]ash)?[:\s]+([a-f0-9]{64})", output)
    if session_match:
        return session_match.group(1)
    return None


def _require_live_services(glaas_available: bool, ray_cluster_available: bool) -> None:
    if not glaas_available:
        pytest.skip("GLaaS not available")
    if not ray_cluster_available:
        pytest.skip("Ray cluster not available")


def _register_model(roar_cli, git_commit) -> str:
    run_result = roar_cli("run", sys.executable, "ray_train.py")
    assert run_result.returncode == 0, run_result.stderr
    git_commit("After ray train")

    reg = roar_cli("register", "model.json")
    assert reg.returncode == 0, reg.stderr

    session_hash = _parse_session_hash(reg.stdout)
    assert session_hash, f"Could not parse session hash from: {reg.stdout}"
    return session_hash


def _get_session_jobs(glaas_client, session_hash: str) -> list[dict]:
    session, error = glaas_client.get_session(session_hash)
    assert error is None, error
    assert session is not None
    jobs = session.get("jobs", [])
    assert isinstance(jobs, list)
    return jobs


def _get_artifact_dag(glaas_client, artifact_hash: str) -> dict:
    dag, error = glaas_client.get_artifact_dag(artifact_hash)
    assert error is None, error
    assert dag is not None
    return dag


def test_ray_jobs_registered(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    ray_script,
    glaas_client,
):
    _require_live_services(glaas_available, ray_cluster_available)

    session_hash = _register_model(roar_cli, git_commit)
    jobs = _get_session_jobs(glaas_client, session_hash)

    assert len(jobs) == 4, f"Expected 4 jobs, got {len(jobs)}: {[j.get('command') for j in jobs]}"
    task_jobs = [j for j in jobs if j.get("jobType") == "ray_task"]
    assert len(task_jobs) == 3, f"Expected 3 ray_task jobs, got {len(task_jobs)}"

    driver_jobs = [j for j in jobs if j.get("jobType") != "ray_task"]
    assert len(driver_jobs) == 1


def test_ray_parent_job_uid_linked(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    ray_script,
    glaas_client,
):
    _require_live_services(glaas_available, ray_cluster_available)

    session_hash = _register_model(roar_cli, git_commit)
    jobs = _get_session_jobs(glaas_client, session_hash)

    driver = next(j for j in jobs if j.get("jobType") != "ray_task")
    tasks = [j for j in jobs if j.get("jobType") == "ray_task"]
    assert len(tasks) == 3, f"Expected 3 ray_task jobs, got {len(tasks)}"

    driver_uid = driver["jobUid"]
    for task in tasks:
        assert task.get("parentJobUid") == driver_uid, (
            f"Task {task['jobUid']} has parentJobUid={task.get('parentJobUid')!r}, "
            f"expected {driver_uid!r}"
        )


def test_ray_dag_nested_children(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    ray_script,
    glaas_client,
):
    _require_live_services(glaas_available, ray_cluster_available)

    _register_model(roar_cli, git_commit)

    lineage = roar_cli("lineage", "model.json")
    assert lineage.returncode == 0, lineage.stderr
    model_hash = json.loads(lineage.stdout)["artifact"]["hash"]

    dag = _get_artifact_dag(glaas_client, model_hash)

    top_jobs = dag.get("jobs", [])
    assert len(top_jobs) > 0, "DAG returned no jobs"

    driver_job = next((job for job in top_jobs if job.get("jobType") != "ray_task"), None)
    assert driver_job is not None, "No driver job in DAG"

    children = driver_job.get("children", [])
    assert len(children) == 3, f"Expected 3 children, got {len(children)}"
    for child in children:
        assert child["command"].startswith("ray_task:"), child["command"]


def test_ray_intermediate_artifacts_in_dag(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    ray_script,
    glaas_client,
):
    _require_live_services(glaas_available, ray_cluster_available)

    _register_model(roar_cli, git_commit)

    lineage = roar_cli("lineage", "model.json")
    assert lineage.returncode == 0, lineage.stderr
    model_hash = json.loads(lineage.stdout)["artifact"]["hash"]

    dag = _get_artifact_dag(glaas_client, model_hash)

    checkpoint_outputs: set[str] = set()
    for job in dag.get("jobs", []):
        if job.get("jobType") == "ray_task":
            continue
        for child in job.get("children", []):
            if child.get("jobType") != "ray_task":
                continue
            for output in child.get("outputs", []):
                path = output.get("path") or ""
                artifact_hash = output.get("artifactHash") or output.get("hash")
                if artifact_hash and "ckpt_" in path and path.endswith(".json"):
                    checkpoint_outputs.add(str(artifact_hash))

    assert len(checkpoint_outputs) > 0, "No task output checkpoint artifacts found in DAG"
    assert len(checkpoint_outputs) == 3, (
        f"Expected 3 checkpoint artifacts, got {len(checkpoint_outputs)}"
    )


def test_ray_register_idempotent(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    ray_script,
    glaas_client,
):
    _require_live_services(glaas_available, ray_cluster_available)

    run_result = roar_cli("run", sys.executable, "ray_train.py")
    assert run_result.returncode == 0, run_result.stderr
    git_commit("After ray train")

    reg1 = roar_cli("register", "model.json")
    assert reg1.returncode == 0, reg1.stderr
    session_hash_1 = _parse_session_hash(reg1.stdout)
    assert session_hash_1

    reg2 = roar_cli("register", "model.json")
    assert reg2.returncode == 0, reg2.stderr
    session_hash_2 = _parse_session_hash(reg2.stdout)
    assert session_hash_2

    assert session_hash_1 == session_hash_2, (
        f"Registration not idempotent: {session_hash_1} != {session_hash_2}"
    )

    jobs = _get_session_jobs(glaas_client, session_hash_1)
    assert len(jobs) == 4


def test_ray_dry_run_shows_counts(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    ray_script,
):
    _require_live_services(glaas_available, ray_cluster_available)

    run_result = roar_cli("run", sys.executable, "ray_train.py")
    assert run_result.returncode == 0, run_result.stderr
    git_commit("After ray train")

    dry = roar_cli("register", "--dry-run", "model.json")
    assert dry.returncode == 0, dry.stderr

    output = dry.stdout
    assert "Jobs:" in output

    count = None
    for line in output.splitlines():
        if "Jobs:" not in line:
            continue
        maybe_count = line.split(":", maxsplit=1)[1].strip()
        if maybe_count.isdigit():
            count = int(maybe_count)
            break

    assert count == 4, f"Expected 4 jobs in dry-run, got {count!r}. Output:\n{output}"
