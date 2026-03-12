"""E2E: `roar run ray job submit` invoked from the LOCAL host against the Docker cluster.

This test file reproduces the cloud topology exactly:
  - `roar run` runs on the local host machine (not inside a container)
  - The Ray cluster (head + workers) runs in Docker containers
  - Workers are isolated processes that cannot reach 127.0.0.1 on the host

These tests cover host-submit behavior that only shows up in the cloud/remote-cluster topology:

  BUG 1 — Worker proxy endpoint unreachable (502 Bad Gateway):
    _ray_job_submit.py hardcodes AWS_ENDPOINT_URL=http://127.0.0.1:19191 into
    the worker runtime env. Workers inside Docker containers or on remote EC2s
    cannot connect to the host's local proxy → all S3 calls fail with 502.

  BUG 2 — Duplicate pip entry:
    _merge_roar_runtime_env_pip() fails to deduplicate URL-based requirements
    (e.g. presigned S3 URLs) because _requirement_name() returns the full URL
    rather than a canonical package name, so the URL is never matched as an
    existing 'roar-cli' entry and gets appended a second time.

The goal is to exercise the real user entrypoint without any roar-aware workload logic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = Path(__file__).resolve().parent / "jobs"
REPO_ROOT = Path(__file__).resolve().parents[3]
ROAR_BIN = REPO_ROOT / ".venv" / "bin" / "roar"
PYTHON_ENV_ROAR_BIN = Path(sys.executable).with_name("roar")
HOST_GLAAS_URL = "http://localhost:3001"
CLUSTER_GLAAS_URL = "http://host.docker.internal:3001"

pytestmark = [pytest.mark.e2e, pytest.mark.ray_contract]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _roar_bin() -> str:
    """Return path to the local roar binary."""
    for candidate in (ROAR_BIN, PYTHON_ENV_ROAR_BIN):
        if candidate.exists():
            return str(candidate)
    # Fallback: rely on PATH (conftest prepends .venv/bin)
    return "roar"


def _init_project(project_dir: Path) -> None:
    """Create a minimal git repo + roar project under project_dir."""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "README.md").write_text("ray host-submit e2e\n", encoding="utf-8")
    (project_dir / ".gitignore").write_text(".roar/\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "README.md", ".gitignore"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [_roar_bin(), "init", "--path", str(project_dir), "-n"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [_roar_bin(), "config", "set", "glaas.url", ""],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )


def _artifact_count(project_dir: Path) -> int:
    """Count S3 artifacts recorded in roar.db after reconstitution."""
    db = project_dir / ".roar" / "roar.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE first_seen_path LIKE 's3://%'"
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _base_env(ray_cluster: dict[str, str]) -> dict[str, str]:
    """Build a clean env for roar run — inherits PATH but overrides AWS/roar vars."""
    env = dict(os.environ)
    env.update(
        {
            # Workers use the Docker image's installed roar — no PyPI download.
            "ROAR_CLUSTER_PIP_REQ": "skip",
            # Minio credentials (Docker compose defaults).
            "AWS_ACCESS_KEY_ID": "minioadmin",
            "AWS_SECRET_ACCESS_KEY": "minioadmin",
            "AWS_DEFAULT_REGION": "us-east-1",
            # Point at the minio instance exposed on the host.
            "AWS_ENDPOINT_URL": str(ray_cluster["minio_endpoint"]),
            # Workers must use the cluster-visible MinIO address, not the host loopback.
            "ROAR_CLUSTER_AWS_ENDPOINT_URL": str(ray_cluster["cluster_minio_endpoint"]),
            # Host submits use the host-visible URL; workers use the cluster-visible URL.
            "GLAAS_URL": HOST_GLAAS_URL,
            "ROAR_CLUSTER_GLAAS_URL": CLUSTER_GLAAS_URL,
        }
    )
    return env


# ---------------------------------------------------------------------------
# Test 1: S3 job succeeds and captures artifacts (proves BUG 1 / proxy routing)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.ray_e2e
def test_roar_run_from_host_s3_job_succeeds_and_captures_artifacts(
    ray_cluster: dict[str, str],
    tmp_path: Path,
) -> None:
    """roar run ray job submit from the LOCAL host must succeed and capture S3 artifacts.

    BUG 1 (currently failing): workers receive AWS_ENDPOINT_URL=http://127.0.0.1:19191
    which points at the host's roar-proxy. Workers inside Docker containers cannot
    reach the host's loopback address, causing every S3 call to fail with 502.

    After the fix (workers get a reachable proxy endpoint), the job should:
      - Complete with exit code 0
      - Record at least 1 S3 artifact in roar.db via proxy log collection
    """
    project_dir = tmp_path / "project"
    _init_project(project_dir)

    cmd = [
        _roar_bin(),
        "run",
        "ray",
        "job",
        "submit",
        "--address",
        ray_cluster["dashboard_url"],
        "--working-dir",
        str(JOBS_DIR),
        "--",
        "python",
        "s3_workload.py",
    ]

    result = subprocess.run(
        cmd,
        cwd=project_dir,
        env=_base_env(ray_cluster),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"roar run ray job submit failed (rc={result.returncode}).\n"
        f"STDOUT:\n{textwrap.indent(result.stdout, '  ')}\n"
        f"STDERR:\n{textwrap.indent(result.stderr, '  ')}"
    )

    artifact_count = _artifact_count(project_dir)
    assert artifact_count > 0, (
        f"Job succeeded but 0 S3 artifacts were captured in roar.db. "
        f"Expected proxy logs to be collected and reconstituted. "
        f"roar.db path: {project_dir / '.roar' / 'roar.db'}"
    )


@pytest.mark.e2e
@pytest.mark.ray_e2e
def test_roar_run_from_host_subprocess_ray_job_captures_s3_artifacts(
    ray_cluster: dict[str, str],
    tmp_path: Path,
) -> None:
    """Ray S3 jobs that exit child subprocesses without ray.shutdown() must still collect logs.

    This matches the cloud-demo topology more closely than the simple workload:
      - a parent driver process spawns child Python processes
      - each child calls ray.init(), performs S3 work, and exits normally
      - no child explicitly calls ray.shutdown()

    The contract is still the same: `roar run ray job submit ...` must capture
    S3 artifacts via the proxy with no workload knowledge of roar.
    """
    project_dir = tmp_path / "project"
    _init_project(project_dir)

    cmd = [
        _roar_bin(),
        "run",
        "ray",
        "job",
        "submit",
        "--address",
        ray_cluster["dashboard_url"],
        "--working-dir",
        str(JOBS_DIR),
        "--",
        "python",
        "s3_subprocess_pipeline.py",
    ]

    result = subprocess.run(
        cmd,
        cwd=project_dir,
        env=_base_env(ray_cluster),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"subprocess Ray job failed (rc={result.returncode}).\n"
        f"STDOUT:\n{textwrap.indent(result.stdout, '  ')}\n"
        f"STDERR:\n{textwrap.indent(result.stderr, '  ')}"
    )

    artifact_count = _artifact_count(project_dir)
    assert artifact_count > 0, (
        "Subprocess Ray job succeeded but 0 S3 artifacts were captured in roar.db. "
        "This means proxy logs were lost when the child processes exited without "
        "calling ray.shutdown()."
    )


# ---------------------------------------------------------------------------
# Test 2: Worker proxy endpoint is reachable from inside the cluster
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.ray_e2e
def test_worker_proxy_endpoint_is_reachable(
    ray_cluster: dict[str, str],
    tmp_path: Path,
) -> None:
    """Workers must be able to connect to the AWS_ENDPOINT_URL they receive.

    BUG 1 (currently failing): AWS_ENDPOINT_URL=http://127.0.0.1:19191 is injected
    into the worker runtime env. Workers inside Docker containers get a loopback
    address that only resolves on the HOST — not on the worker's own network
    namespace — so socket.connect() fails with ECONNREFUSED.

    After the fix, every worker should report reachable=True.
    """
    project_dir = tmp_path / "project"
    _init_project(project_dir)

    cmd = [
        _roar_bin(),
        "run",
        "ray",
        "job",
        "submit",
        "--address",
        ray_cluster["dashboard_url"],
        "--working-dir",
        str(JOBS_DIR),
        "--",
        "python",
        "proxy_reachability_probe.py",
    ]

    result = subprocess.run(
        cmd,
        cwd=project_dir,
        env=_base_env(ray_cluster),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"Probe job failed (rc={result.returncode}).\n"
        f"STDOUT:\n{textwrap.indent(result.stdout, '  ')}\n"
        f"STDERR:\n{textwrap.indent(result.stderr, '  ')}"
    )

    # Parse the JSON output from the probe — it's emitted to stdout by the job driver.
    probe_output: dict | None = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('{"results"'):
            try:
                probe_output = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    assert probe_output is not None, f"Could not find probe JSON output in stdout:\n{result.stdout}"

    worker_results: list[dict] = probe_output["results"]
    unreachable = [r for r in worker_results if not r.get("reachable")]

    assert not unreachable, (
        f"{len(unreachable)}/{len(worker_results)} workers could not reach their proxy endpoint.\n"
        + "\n".join(
            f"  node={r['node_id'][:8]} endpoint={r['endpoint']} error={r['error']}"
            for r in unreachable
        )
    )


# ---------------------------------------------------------------------------
# Test 3: Runtime env pip list has no duplicates (proves BUG 2)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.ray_e2e
def test_roar_run_runtime_env_pip_no_duplicates(
    ray_cluster: dict[str, str],
    tmp_path: Path,
) -> None:
    """The pip list injected into runtime_env must not contain duplicate entries.

    BUG 2 (currently failing): when ROAR_CLUSTER_PIP_REQ is a URL-based requirement
    (e.g. a presigned S3 URL), _merge_roar_runtime_env_pip() fails to recognise it
    as an existing 'roar-cli' entry (because _requirement_name() returns the full URL).
    If the user's runtime_env already contains the URL, it ends up duplicated.

    This test passes ROAR_CLUSTER_PIP_REQ=<url> and a pre-populated pip list
    containing the same URL, then inspects the runtime-env JSON that roar injects
    into the ray job submit command.
    """
    FAKE_WHEEL_URL = (
        "https://example.com/wheels/roar_cli-0.2.12-cp312-cp312-linux_x86_64.whl"
        "?X-Amz-Signature=deadbeef"
    )

    project_dir = tmp_path / "project"
    _init_project(project_dir)

    # A pre-existing runtime env that already has the wheel URL in pip.
    existing_runtime_env = json.dumps(
        {
            "pip": [FAKE_WHEEL_URL],
            "env_vars": {},
        }
    )

    env = _base_env(ray_cluster)
    env["ROAR_CLUSTER_PIP_REQ"] = FAKE_WHEEL_URL

    # Use --dry-run flag is not available, so we intercept via a no-op entrypoint
    # and inspect the --runtime-env-json that roar passes through.
    # Strategy: write a tiny Python script that captures sys.argv and exits 0.
    spy_script = tmp_path / "spy.py"
    spy_script.write_text(
        textwrap.dedent("""\
        import sys, json, pathlib
        args = sys.argv[1:]
        pathlib.Path('/tmp/roar_spy_args.json').write_text(json.dumps(args))
        raise SystemExit(0)
    """)
    )

    # Patch 'ray' in PATH to the spy script so roar run intercepts the final command.
    fake_ray_dir = tmp_path / "fakebin"
    fake_ray_dir.mkdir()
    fake_ray = fake_ray_dir / "ray"
    fake_ray.write_text(f'#!/bin/sh\nexec {sys.executable} {spy_script} "$@"\n')
    fake_ray.chmod(0o755)

    patched_env = dict(env)
    patched_env["PATH"] = f"{fake_ray_dir}:{patched_env.get('PATH', '')}"

    cmd = [
        _roar_bin(),
        "run",
        "ray",
        "job",
        "submit",
        "--address",
        ray_cluster["dashboard_url"],
        "--runtime-env-json",
        existing_runtime_env,
        "--working-dir",
        str(JOBS_DIR),
        "--",
        "python",
        "s3_workload.py",
    ]

    subprocess.run(
        cmd,
        cwd=project_dir,
        env=patched_env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    spy_output = Path("/tmp/roar_spy_args.json")
    assert spy_output.exists(), "Spy script was never invoked — roar run did not call ray"

    captured_args = json.loads(spy_output.read_text())

    # Extract --runtime-env-json value from the captured args.
    runtime_env_json: str | None = None
    for i, arg in enumerate(captured_args):
        if arg == "--runtime-env-json" and i + 1 < len(captured_args):
            runtime_env_json = captured_args[i + 1]
            break

    assert runtime_env_json is not None, (
        f"--runtime-env-json not found in captured args: {captured_args}"
    )

    runtime_env = json.loads(runtime_env_json)
    pip_list: list[str] = runtime_env.get("pip", [])

    # Normalise for comparison (strip whitespace).
    normalised = [p.strip() for p in pip_list]
    duplicates = [p for p in normalised if normalised.count(p) > 1]

    assert not duplicates, (
        "Duplicate pip entries found in runtime_env:\n"
        + "\n".join(f"  {p}" for p in sorted(set(duplicates)))
        + f"\nFull pip list: {pip_list}"
    )
