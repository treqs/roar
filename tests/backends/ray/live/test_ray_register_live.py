"""Live integration coverage for GLaaS registration of Ray jobs."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_glaas

HOST_SUBMIT_JOBS_DIR = Path(__file__).resolve().parents[1] / "e2e" / "jobs"
RAY_JOB_ADDRESS = "http://localhost:8265"


@pytest.fixture(scope="module")
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
    subprocess.run(
        [sys.executable, "-m", "roar", "config", "set", "ray.pip_install", "false"],
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


@pytest.fixture(scope="module")
def ray_job_submit_available() -> bool:
    try:
        req = urllib.request.Request(f"{RAY_JOB_ADDRESS}/api/version")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture
def glaas_client(glaas_url: str):
    from roar.glaas_client import GlaasClient

    return GlaasClient(glaas_url)


@pytest.fixture(scope="module")
def glaas_session_read_available(glaas_url: str) -> bool:
    from roar.glaas_client import make_auth_header

    session_hash = "0" * 64
    api_path = f"/api/v1/sessions/{session_hash}"
    headers = [None]

    auth_header = make_auth_header("GET", api_path)
    if auth_header:
        headers.append(auth_header)

    for header in headers:
        req = urllib.request.Request(f"{glaas_url}{api_path}")
        if header:
            req.add_header("Authorization", header)
        try:
            with urllib.request.urlopen(req, timeout=5):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                return True
        except Exception:
            return False

    return False


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
    try:
        import roar.backends.ray.roar_worker as _roar_worker
        _roar_worker._startup()
    except Exception:
        pass
    out = f"ckpt_{shard_id}.json"
    payload = {"shard": shard_id, "processed": data.upper()}
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return {"checkpoint": os.path.abspath(out), "payload": payload}

ray.init(address="auto")
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


def _require_host_submit_services(
    glaas_available: bool,
    ray_cluster_available: bool,
    ray_job_submit_available: bool,
) -> None:
    _require_live_services(glaas_available, ray_cluster_available)
    if not ray_job_submit_available:
        pytest.skip("Ray dashboard job submit API not available")


def _require_live_session_read(glaas_session_read_available: bool) -> None:
    if not glaas_session_read_available:
        pytest.skip("GLaaS session read endpoint currently requires unavailable authorization")


def _query_roar_db(repo: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(repo / ".roar" / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query, params)
        return cur.fetchall()
    finally:
        conn.close()


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


def _active_session_id(repo: Path) -> int:
    rows = _query_roar_db(
        repo,
        """
        SELECT id
        FROM sessions
        WHERE is_active = 1
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    row = rows[0] if rows else None
    assert row is not None, "Expected an active local roar session after host submit"
    return int(row[0])


def _compute_active_session_hash(repo: Path) -> str:
    from roar.services.registration.session import SessionRegistrationService

    session_id = _active_session_id(repo)
    return SessionRegistrationService().compute_session_hash(
        roar_dir=str(repo / ".roar"),
        session_id=session_id,
    )


def _register_active_session_hash(repo: Path, roar_cli) -> str:
    session_hash = _compute_active_session_hash(repo)
    register_result = roar_cli("register", session_hash, "--yes")
    assert register_result.returncode == 0, register_result.stderr
    assert _parse_session_hash(register_result.stdout) == session_hash
    return session_hash


def _latest_artifact_hash(repo: Path, path_pattern: str, *, algorithm: str = "blake3") -> str:
    rows = _query_roar_db(
        repo,
        """
        SELECT ah.digest
        FROM artifacts a
        JOIN artifact_hashes ah ON ah.artifact_id = a.id
        WHERE ah.algorithm = ?
          AND COALESCE(a.path, a.first_seen_path) LIKE ?
        ORDER BY a.first_seen_at DESC
        LIMIT 1
        """,
        (algorithm, path_pattern),
    )
    assert rows, f"Expected artifact matching {path_pattern!r} in local roar.db"
    return str(rows[0]["digest"])


def _run_host_submit(
    repo: Path,
    *,
    glaas_url: str,
    working_dir: Path,
    entrypoint: list[str],
    runtime_env: dict[str, object] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    candidate = Path(sys.executable).with_name("ray")
    ray_binary = str(candidate) if candidate.exists() else shutil.which("ray")
    if not ray_binary:
        pytest.skip("ray CLI is not available in PATH")

    env = dict(os.environ)
    env.update(
        {
            "GLAAS_URL": glaas_url,
            "ROAR_CLUSTER_GLAAS_URL": os.environ.get(
                "ROAR_CLUSTER_GLAAS_URL",
                "http://host.docker.internal:3001",
            ),
        }
    )

    command = [
        sys.executable,
        "-m",
        "roar",
        "run",
        "--tracer",
        "ptrace",
        ray_binary,
        "job",
        "submit",
        "--address",
        RAY_JOB_ADDRESS,
        "--working-dir",
        str(working_dir),
    ]
    if runtime_env is not None:
        command.extend(["--runtime-env-json", json.dumps(runtime_env)])
    command.extend(["--", *entrypoint])

    return subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def _run_host_submit_basic_file_io(
    repo: Path, *, glaas_url: str
) -> subprocess.CompletedProcess[str]:
    return _run_host_submit(
        repo,
        glaas_url=glaas_url,
        working_dir=HOST_SUBMIT_JOBS_DIR,
        entrypoint=["python", "basic_file_io.py"],
        runtime_env={"pip": ["pydantic==2.12.5", "pydantic-settings==2.12.0"]},
    )


def _run_host_submit_training(repo: Path, *, glaas_url: str) -> subprocess.CompletedProcess[str]:
    return _run_host_submit(
        repo,
        glaas_url=glaas_url,
        working_dir=repo,
        entrypoint=["python", "ray_train.py"],
        timeout=240,
    )


def _submit_and_register_training_session(repo: Path, roar_cli, *, glaas_url: str) -> str:
    run_result = _run_host_submit_training(repo, glaas_url=glaas_url)
    assert run_result.returncode == 0, (
        "Expected host-submit Ray training job to succeed.\n"
        f"stdout:\n{run_result.stdout}\n\nstderr:\n{run_result.stderr}"
    )
    return _register_active_session_hash(repo, roar_cli)


def test_ray_jobs_registered(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    ray_job_submit_available,
    glaas_session_read_available,
    roar_cli,
    glaas_url,
    ray_script,
    glaas_client,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )
    _require_live_session_read(glaas_session_read_available)

    del ray_script
    session_hash = _submit_and_register_training_session(
        glaas_configured,
        roar_cli,
        glaas_url=glaas_url,
    )
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
    ray_job_submit_available,
    glaas_session_read_available,
    roar_cli,
    glaas_url,
    ray_script,
    glaas_client,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )
    _require_live_session_read(glaas_session_read_available)

    del ray_script
    session_hash = _submit_and_register_training_session(
        glaas_configured,
        roar_cli,
        glaas_url=glaas_url,
    )
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
    ray_job_submit_available,
    glaas_session_read_available,
    roar_cli,
    glaas_url,
    ray_script,
    glaas_client,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )
    _require_live_session_read(glaas_session_read_available)

    del ray_script
    _submit_and_register_training_session(
        glaas_configured,
        roar_cli,
        glaas_url=glaas_url,
    )
    model_hash = _latest_artifact_hash(glaas_configured, "%model.json")

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
    ray_job_submit_available,
    glaas_session_read_available,
    roar_cli,
    glaas_url,
    ray_script,
    glaas_client,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )
    _require_live_session_read(glaas_session_read_available)

    del ray_script
    _submit_and_register_training_session(
        glaas_configured,
        roar_cli,
        glaas_url=glaas_url,
    )
    model_hash = _latest_artifact_hash(glaas_configured, "%model.json")

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
    ray_job_submit_available,
    glaas_session_read_available,
    roar_cli,
    glaas_url,
    ray_script,
    glaas_client,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )
    _require_live_session_read(glaas_session_read_available)

    del ray_script
    run_result = _run_host_submit_training(glaas_configured, glaas_url=glaas_url)
    assert run_result.returncode == 0, run_result.stderr
    session_hash = _compute_active_session_hash(glaas_configured)

    reg1 = roar_cli("register", session_hash, "--yes")
    assert reg1.returncode == 0, reg1.stderr
    session_hash_1 = _parse_session_hash(reg1.stdout)
    assert session_hash_1

    reg2 = roar_cli("register", session_hash, "--yes")
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
    ray_job_submit_available,
    roar_cli,
    glaas_url,
    ray_script,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )

    del ray_script
    run_result = _run_host_submit_training(glaas_configured, glaas_url=glaas_url)
    assert run_result.returncode == 0, run_result.stderr
    session_hash = _compute_active_session_hash(glaas_configured)

    dry = roar_cli("register", "--dry-run", session_hash)
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


def test_ray_host_submit_registers_without_runtime_env_override_when_pip_install_disabled(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    ray_job_submit_available,
    glaas_session_read_available,
    roar_cli,
    glaas_client,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )
    _require_live_session_read(glaas_session_read_available)

    run_result = _run_host_submit_basic_file_io(
        glaas_configured,
        glaas_url=os.environ.get("GLAAS_URL", "http://localhost:3001"),
    )
    assert run_result.returncode == 0, (
        "Expected host-submit `roar run ray job submit` to succeed with user-provided "
        "runtime_env.pip when ray.pip_install=false.\n"
        f"stdout:\n{run_result.stdout}\n\nstderr:\n{run_result.stderr}"
    )

    session_hash = _register_active_session_hash(glaas_configured, roar_cli)
    jobs = _get_session_jobs(glaas_client, session_hash)
    task_jobs = [job for job in jobs if job.get("jobType") == "ray_task"]
    assert task_jobs, f"Expected at least one registered ray_task job, got: {jobs}"


def test_ray_host_submit_register_command_succeeds_when_pip_install_disabled(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    ray_job_submit_available,
    roar_cli,
):
    _require_host_submit_services(
        glaas_available,
        ray_cluster_available,
        ray_job_submit_available,
    )

    run_result = _run_host_submit_basic_file_io(
        glaas_configured,
        glaas_url=os.environ.get("GLAAS_URL", "http://localhost:3001"),
    )
    assert run_result.returncode == 0, (
        "Expected host-submit `roar run ray job submit` to succeed with user-provided "
        "runtime_env.pip when ray.pip_install=false.\n"
        f"stdout:\n{run_result.stdout}\n\nstderr:\n{run_result.stderr}"
    )
    session_hash = _register_active_session_hash(glaas_configured, roar_cli)
    assert len(session_hash) == 64
