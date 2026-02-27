"""Live GLaaS coverage for the 3-stage S3 Ray pipeline."""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from roar.glaas_client import make_auth_header

pytestmark = pytest.mark.live_glaas

PIPELINE_SCRIPT = Path(__file__).resolve().parents[1] / "e2e" / "ray" / "jobs" / "s3_pipeline.py"


@pytest.fixture
def glaas_url() -> str:
    return os.environ.get("GLAAS_URL", "http://localhost:3001")


@pytest.fixture
def glaas_available(glaas_url: str) -> bool:
    try:
        req = urllib.request.Request(f"{glaas_url}/api/v1/health")
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


@pytest.fixture
def ray_cluster_available() -> bool:
    try:
        conn = socket.create_connection(("localhost", 10001), timeout=3)
        conn.close()
        return True
    except OSError:
        return False


@pytest.fixture
def glaas_configured(temp_git_repo: Path, glaas_url: str) -> Path:
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


@pytest.fixture
def glaas_http(glaas_url: str) -> Callable[[str], dict[str, Any]]:
    def _request(api_path: str) -> dict[str, Any]:
        req = urllib.request.Request(f"{glaas_url}{api_path}")
        auth_header = make_auth_header("GET", api_path)
        if auth_header:
            req.add_header("Authorization", auth_header)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = response.read().decode()
                if not payload:
                    return {}
                obj = json.loads(payload)
                if isinstance(obj, dict) and isinstance(obj.get("data"), dict):
                    return obj["data"]
                return obj
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise AssertionError(f"GET {api_path} failed with HTTP {exc.code}: {body}") from exc

    return _request


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")


def _require_live_services(glaas_available: bool, ray_cluster_available: bool) -> None:
    if not glaas_available:
        pytest.skip("GLaaS not available")
    if not ray_cluster_available:
        pytest.skip("Ray cluster not available")


def _parse_run_info(stdout: str) -> dict[str, str]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_id = payload.get("run_id")
        report_key = payload.get("report_key")
        if isinstance(run_id, str) and isinstance(report_key, str):
            return {"run_id": run_id, "report_key": report_key}
    raise AssertionError(f"Could not parse run info from output:\n{stdout}")


def _parse_session_hash(output: str) -> str | None:
    url_match = re.search(r"/dag/([a-f0-9]{64})", output)
    if url_match:
        return url_match.group(1)
    session_match = re.search(r"[Ss]ession(?:\s+[Hh]ash)?[:\s]+([a-f0-9]{64})", output)
    if session_match:
        return session_match.group(1)
    return None


def _parse_registered_artifact_hash(output: str) -> str | None:
    by_url = re.search(r"/artifact/([A-Za-z0-9_-]{8,})", output)
    if by_url:
        return by_url.group(1)
    by_reproduce = re.search(r"roar reproduce ([A-Za-z0-9_-]{8,})", output)
    if by_reproduce:
        return by_reproduce.group(1)
    return None


def _lookup_artifact_hash(roar_repo: Path, artifact_path: str) -> str:
    conn = sqlite3.connect(roar_repo / ".roar" / "roar.db")
    try:
        row = conn.execute(
            """
            SELECT ah.digest
            FROM artifacts a
            JOIN artifact_hashes ah ON ah.artifact_id = a.id
            WHERE a.path = ?
            ORDER BY
                CASE ah.algorithm
                    WHEN 'blake3' THEN 0
                    WHEN 'etag' THEN 1
                    WHEN 'sha256' THEN 2
                    ELSE 99
                END,
                a.first_seen_at DESC
            LIMIT 1
            """,
            (artifact_path,),
        ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise AssertionError(f"No hash found for artifact path: {artifact_path}")
        return row[0]
    finally:
        conn.close()


def _run_pipeline_and_register(
    repo: Path,
    roar_cli,
    git_commit,
) -> tuple[dict[str, str], str, str]:
    run_result = roar_cli("run", sys.executable, str(PIPELINE_SCRIPT), "ray://localhost:10001")
    assert run_result.returncode == 0, run_result.stderr
    run_info = _parse_run_info(run_result.stdout)
    git_commit("After s3 pipeline")

    report_path = f"s3://output-bucket/results/{run_info['run_id']}/final_report.json"
    register_result = roar_cli("register", report_path)
    assert register_result.returncode == 0, register_result.stderr

    session_hash = _parse_session_hash(register_result.stdout)
    assert session_hash, f"Could not parse session hash from:\n{register_result.stdout}"

    artifact_hash = _parse_registered_artifact_hash(register_result.stdout)
    if not artifact_hash:
        artifact_hash = _lookup_artifact_hash(repo, report_path)
    return run_info, session_hash, artifact_hash


def _jobs_total(payload: dict[str, Any]) -> int:
    jobs = payload.get("jobs", [])
    return len(jobs) if isinstance(jobs, list) else 0


def _walk_artifact_tree(
    node: dict[str, Any],
    *,
    parent_job_command: str | None = None,
) -> list[tuple[dict[str, Any], str | None]]:
    out: list[tuple[dict[str, Any], str | None]] = []
    if not isinstance(node, dict):
        return out

    out.append((node, parent_job_command))
    produced_by = node.get("producedBy")
    current_job_command = (
        str(produced_by.get("command"))
        if isinstance(produced_by, dict) and isinstance(produced_by.get("command"), str)
        else None
    )

    inputs = node.get("inputs", [])
    if isinstance(inputs, list):
        for child in inputs:
            if isinstance(child, dict):
                out.extend(_walk_artifact_tree(child, parent_job_command=current_job_command))
    return out


def _max_artifact_depth(node: dict[str, Any]) -> int:
    if not isinstance(node, dict):
        return 0
    children = node.get("inputs", [])
    if not isinstance(children, list) or not children:
        return 1
    return 1 + max(_max_artifact_depth(child) for child in children if isinstance(child, dict))


def test_s3_pipeline_session_has_correct_job_count(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    glaas_http,
    s3_env,
):
    del s3_env
    _require_live_services(glaas_available, ray_cluster_available)

    run_info, session_hash, _final_report_hash = _run_pipeline_and_register(
        glaas_configured,
        roar_cli,
        git_commit,
    )
    del run_info

    session = glaas_http(f"/api/v1/sessions/{session_hash}")
    assert _jobs_total(session) == 10, f"Expected 10 jobs, got {_jobs_total(session)}: {session}"
    task_jobs = [job for job in session.get("jobs", []) if job.get("jobType") == "ray_task"]
    assert len(task_jobs) == 9


def test_s3_pipeline_dag_depth(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    glaas_http,
    s3_env,
):
    del s3_env
    _require_live_services(glaas_available, ray_cluster_available)

    _run_info, _session_hash, final_report_hash = _run_pipeline_and_register(
        glaas_configured,
        roar_cli,
        git_commit,
    )

    lineage = glaas_http(f"/api/v1/artifacts/{final_report_hash}/lineage?depth=8")
    assert _max_artifact_depth(lineage) >= 4

    seen_commands = {
        str(node.get("producedBy", {}).get("command", ""))
        for node, _parent_cmd in _walk_artifact_tree(lineage)
        if isinstance(node.get("producedBy"), dict)
    }
    commands = {command for command in seen_commands if command}
    assert any("ingest_shard" in command for command in commands)
    assert any("train_shard" in command for command in commands)
    assert any("eval_model" in command for command in commands)


def test_intermediate_s3_artifact_shows_cross_task_lineage(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    glaas_http,
    s3_env,
):
    del s3_env
    _require_live_services(glaas_available, ray_cluster_available)

    run_info, _session_hash, _final_report_hash = _run_pipeline_and_register(
        glaas_configured,
        roar_cli,
        git_commit,
    )
    processed_path = f"s3://test-bucket/processed/{run_info['run_id']}/shard_0.json"
    processed_hash = _lookup_artifact_hash(glaas_configured, processed_path)

    final_report_hash = _lookup_artifact_hash(
        glaas_configured,
        f"s3://output-bucket/results/{run_info['run_id']}/final_report.json",
    )
    lineage = glaas_http(f"/api/v1/artifacts/{final_report_hash}/lineage?depth=8")
    entries = _walk_artifact_tree(lineage)

    processed_entries = [entry for entry in entries if entry[0].get("hash") == processed_hash]
    assert processed_entries, f"Processed shard not found in final report lineage: {processed_path}"

    producer_commands = {
        str(entry[0].get("producedBy", {}).get("command", ""))
        for entry in processed_entries
        if isinstance(entry[0].get("producedBy"), dict)
    }
    consumer_commands = {str(parent_cmd) for _node, parent_cmd in processed_entries if parent_cmd}

    assert any("ingest_shard" in command for command in producer_commands)
    assert any("train_shard" in command for command in consumer_commands)


def test_s3_artifacts_have_etag_hashes(
    glaas_configured,
    glaas_available,
    ray_cluster_available,
    roar_cli,
    git_commit,
    glaas_http,
    s3_env,
):
    del s3_env
    _require_live_services(glaas_available, ray_cluster_available)

    _run_info, session_hash, _final_report_hash = _run_pipeline_and_register(
        glaas_configured,
        roar_cli,
        git_commit,
    )

    session = glaas_http(f"/api/v1/sessions/{session_hash}")
    jobs = session.get("jobs", [])
    assert len(jobs) == 10

    final_report_job = next(
        (job for job in jobs if "s3_pipeline.py" in str(job.get("command", ""))),
        None,
    )
    assert final_report_job is not None

    final_report_hash = _lookup_artifact_hash(
        glaas_configured,
        f"s3://output-bucket/results/{_run_info['run_id']}/final_report.json",
    )
    lineage = glaas_http(f"/api/v1/artifacts/{final_report_hash}/lineage?depth=8")

    by_hash: dict[str, dict[str, Any]] = {}
    for node, _parent_cmd in _walk_artifact_tree(lineage):
        node_hash = node.get("hash")
        if isinstance(node_hash, str) and node_hash:
            by_hash[node_hash] = node

    s3_artifacts = [
        node for node in by_hash.values() if str(node.get("sourceType", "")).lower() == "s3"
    ]
    assert len(s3_artifacts) >= 13
    for artifact in s3_artifacts:
        assert isinstance(artifact.get("hash"), str) and artifact["hash"]
