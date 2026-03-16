"""E2E gap-verification tests for Ray S3 tracking via `roar run ray job submit`."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

RAY_DASHBOARD_URL = "http://localhost:8265/api/version"
GLAAS_HEALTH_URL = "http://localhost:3001/api/v1/health"
GLAAS_BASE_URL = "http://localhost:3001"
RAY_JOB_ADDRESS = "http://localhost:8265"
REPO_ROOT = Path(__file__).resolve().parents[4]
JOBS_DIR = Path("tests/backends/ray/e2e/jobs")

pytestmark = pytest.mark.e2e


def _http_get(url: str, timeout_seconds: int = 5) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        status = int(response.getcode())
        body = response.read().decode("utf-8", errors="replace")
        return status, body


def _skip_if_services_unreachable() -> None:
    checks = (
        ("Ray dashboard", RAY_DASHBOARD_URL),
        ("GLaaS", GLAAS_HEALTH_URL),
    )
    for service_name, url in checks:
        try:
            status, _body = _http_get(url)
        except urllib.error.URLError as exc:
            pytest.skip(f"{service_name} not reachable at {url}: {exc}")
        except (TimeoutError, ConnectionError, OSError) as exc:
            pytest.skip(f"{service_name} not reachable at {url}: {exc}")
        if status != 200:
            pytest.skip(f"{service_name} not healthy at {url}: HTTP {status}")


def _run_checked(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(
            f"Command failed ({' '.join(command)}):\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )


def _init_clean_project(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "README.md").write_text("ray s3 tracking gaps e2e\n", encoding="utf-8")
    (project_dir / ".gitignore").write_text(".roar/\n", encoding="utf-8")

    _run_checked(["git", "init"], cwd=project_dir)
    _run_checked(["git", "config", "user.email", "e2e@example.com"], cwd=project_dir)
    _run_checked(["git", "config", "user.name", "E2E"], cwd=project_dir)
    _run_checked(["git", "add", "README.md", ".gitignore"], cwd=project_dir)
    _run_checked(["git", "commit", "-m", "init"], cwd=project_dir)
    _run_checked(
        [sys.executable, "-m", "roar", "init", "--path", str(project_dir), "-n"], cwd=project_dir
    )

    config_path = project_dir / ".roar" / "config.toml"
    config_text = config_path.read_text(encoding="utf-8")
    if 'url = "https://api.glaas.ai"' in config_text:
        config_text = config_text.replace(
            'url = "https://api.glaas.ai"',
            f'url = "{GLAAS_BASE_URL}"',
        )
    config_path.write_text(config_text, encoding="utf-8")


def _maybe_skip_transient_submit_failure(result: subprocess.CompletedProcess[str]) -> None:
    output = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode != 0 and "require the ray[default] installation" in output:
        pytest.skip("Ray job submit requires ray[default] in this environment")
    if result.returncode != 0 and any(
        marker in output
        for marker in (
            "connection refused",
            "failed to connect",
            "unable to connect",
            "cannot connect",
            "timed out",
            "deadline exceeded",
        )
    ):
        pytest.skip("Ray or GLaaS became unreachable during submit")


def _extract_json_objects(output: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for index, char in enumerate(line):
            if char != "{":
                continue
            try:
                maybe_payload = json.loads(line[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(maybe_payload, dict):
                payloads.append(maybe_payload)
                break
    return payloads


def _require_payload(
    output: str,
    predicate: Callable[[dict[str, Any]], bool],
    label: str,
) -> dict[str, Any]:
    payloads = [payload for payload in _extract_json_objects(output) if predicate(payload)]
    assert payloads, f"{label}: unable to find matching JSON payload in submit output:\n{output}"
    return payloads[-1]


def _submit_job(
    project_dir: Path,
    script_name: str,
    *,
    script_args: list[str] | None = None,
    runtime_env_vars: dict[str, str] | None = None,
    tracer: str | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "roar", "run"]
    if tracer:
        command.extend(["--tracer", tracer])
    command.extend(
        [
            "ray",
            "job",
            "submit",
            "--address",
            RAY_JOB_ADDRESS,
            "--working-dir",
            str(REPO_ROOT),
        ]
    )
    if runtime_env_vars:
        command.extend(
            [
                "--runtime-env-json",
                json.dumps({"env_vars": runtime_env_vars}, separators=(",", ":")),
            ]
        )
    command.extend(
        [
            "--",
            "python3",
            str(JOBS_DIR / script_name),
        ]
    )
    if script_args:
        command.extend(script_args)

    env = dict(os.environ)
    env.setdefault("GLAAS_URL", GLAAS_BASE_URL)
    env.setdefault("GLAAS_API_URL", GLAAS_BASE_URL)
    env.setdefault("RAY_OVERRIDE_JOB_RUNTIME_ENV", "1")

    result = subprocess.run(
        command,
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )
    _maybe_skip_transient_submit_failure(result)
    return result


def _query_project_db(
    project_dir: Path, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    db_path = project_dir / ".roar" / "roar.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Expected local roar DB at {db_path}, but it does not exist.")
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _manual_gap_runtime_env(job_id: str) -> dict[str, str]:
    return {
        "ROAR_EXECUTION_BACKEND": "ray",
        "ROAR_JOB_ID": job_id,
        "ROAR_RAY_NODE_AGENTS": "1",
        "ROAR_WRAP": "1",
        "PYTHONPATH": "/app/roar/execution/runtime/inject",
        "AWS_ENDPOINT_URL": "",
    }


def _query_s3_lineage_rows(project_dir: Path, path_like: str) -> list[dict[str, Any]]:
    return _query_project_db(
        project_dir,
        """
        SELECT io_kind,
               path,
               COALESCE(hash, '') AS hash,
               COALESCE(size, 0) AS size,
               COALESCE(capture_method, '') AS capture_method
        FROM (
            SELECT 'read' AS io_kind, a.path, a.hash, a.size, a.capture_method
            FROM job_inputs ji
            JOIN artifacts a ON a.id = ji.artifact_id
            UNION ALL
            SELECT 'write' AS io_kind, a.path, a.hash, a.size, a.capture_method
            FROM job_outputs jo
            JOIN artifacts a ON a.id = jo.artifact_id
        )
        WHERE path LIKE ?
        ORDER BY io_kind, path
        """,
        (path_like,),
    )


def _query_s3_hash_rows(project_dir: Path, path_like: str) -> list[dict[str, Any]]:
    return _query_project_db(
        project_dir,
        """
        SELECT a.path, ah.algorithm, ah.digest
        FROM artifact_hashes ah
        JOIN artifacts a ON a.id = ah.artifact_id
        WHERE a.path LIKE ?
        ORDER BY a.path, ah.algorithm
        """,
        (path_like,),
    )


def _latest_fragment_session(project_dir: Path) -> tuple[str, str]:
    fragment_dir = project_dir / ".roar" / "fragment-sessions"
    key_files = sorted(fragment_dir.glob("*.key"), key=lambda path: path.stat().st_mtime)
    if not key_files:
        return "", ""

    key_payload = json.loads(key_files[-1].read_text(encoding="utf-8"))
    session_id = str(key_payload.get("session_id", ""))
    token = str(key_payload.get("token", ""))
    return session_id, token


def _fetch_fragment_batches(session_id: str, token: str) -> list[dict[str, Any]]:
    if not session_id or not token:
        return []

    request = urllib.request.Request(
        url=f"{GLAAS_BASE_URL}/api/v1/fragments/sessions/{session_id}/fragments",
        headers={"x-roar-fragment-token": token},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    rows = payload.get("data", {}).get("fragments", payload.get("fragments", []))
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _decrypt_fragment_batches(token: str, batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not token or not batches:
        return []

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = bytes.fromhex(token)
    aesgcm = AESGCM(key)
    fragments: list[dict[str, Any]] = []

    def _sequence_key(row: dict[str, Any]) -> int:
        raw = row.get("sequence")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 2**31 - 1

    for row in sorted(batches, key=_sequence_key):
        encrypted_batch = row.get("encrypted_batch")
        if not isinstance(encrypted_batch, str) or not encrypted_batch:
            continue

        payload = base64.b64decode(encrypted_batch)
        if len(payload) <= 12:
            continue
        plaintext = aesgcm.decrypt(payload[:12], payload[12:], None)
        decoded = json.loads(plaintext.decode("utf-8"))
        if isinstance(decoded, list):
            fragments.extend(item for item in decoded if isinstance(item, dict))

    return fragments


def _s3_fragment_entries(fragments: list[dict[str, Any]]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for fragment in fragments:
        for io_kind, field in (("read", "reads"), ("write", "writes")):
            refs = fragment.get(field, [])
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                path = str(ref.get("path", ""))
                if not path.startswith("s3://"):
                    continue
                entries.append(
                    {
                        "io_kind": io_kind,
                        "path": path,
                        "capture_method": str(ref.get("capture_method", "")),
                    }
                )
    return entries


@pytest.fixture
def roar_project(tmp_path: Path, ray_cluster: dict[str, str]) -> Path:
    del ray_cluster
    _skip_if_services_unreachable()
    project_dir = tmp_path / "repo"
    _init_clean_project(project_dir)
    return project_dir


class TestP0Gaps:
    def test_g1_sentinel_path_skips_node_agents(self, roar_project: Path) -> None:
        job_id = f"gap1-{uuid.uuid4().hex[:8]}"
        result = _submit_job(
            roar_project,
            "roar_diagnostic_probe.py",
            script_args=["--check", "node-agents"],
            runtime_env_vars=_manual_gap_runtime_env(job_id),
            tracer="ptrace",
        )
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "Gap G1: diagnostic probe should run successfully so node-agent spawn behavior can be "
            f"validated. submit output:\n{output}"
        )

        payload = _require_payload(
            output,
            lambda item: item.get("check") == "node-agents",
            "Gap G1 node-agent diagnostics",
        )
        alive_nodes = [item for item in payload.get("alive_nodes", []) if isinstance(item, dict)]
        node_agents_found_count = int(payload.get("node_agents_found_count", -1))
        missing_agent_names = [
            str(name)
            for name in payload.get("missing_agent_names", [])
            if isinstance(name, str) and name
        ]

        assert alive_nodes, "Gap G1: expected at least one alive Ray node in diagnostic payload."
        assert node_agents_found_count == len(alive_nodes), (
            "Gap G1: sentinel path should spawn one node-agent actor per alive node, "
            f"but found {node_agents_found_count} node agents for {len(alive_nodes)} alive nodes."
        )
        assert not missing_agent_names, (
            "Gap G1: no node-agent actor names should be missing once the sentinel path spawns agents, "
            f"but missing actors were reported: {missing_agent_names}"
        )

        expected_agent_names = {
            str(name)
            for name in payload.get("expected_agent_names", [])
            if isinstance(name, str) and name
        }
        actors = [item for item in payload.get("actors", []) if isinstance(item, dict)]
        alive_actor_names = {
            str(item.get("name", ""))
            for item in actors
            if str(item.get("state", "")).upper() == "ALIVE" and str(item.get("name", ""))
        }
        assert expected_agent_names and expected_agent_names.issubset(alive_actor_names), (
            "Gap G1: all expected node-agent actors should appear in Ray state as ALIVE, "
            f"but expected={sorted(expected_agent_names)} alive={sorted(alive_actor_names)}"
        )

    def test_g2_worker_startup_missing_local_proxy_endpoint(self, roar_project: Path) -> None:
        job_id = f"gap2-{uuid.uuid4().hex[:8]}"
        result = _submit_job(
            roar_project,
            "roar_diagnostic_probe.py",
            script_args=["--check", "node-agents,proxy-env"],
            runtime_env_vars=_manual_gap_runtime_env(job_id),
            tracer="ptrace",
        )
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "Gap G2: diagnostic probe should run successfully so worker proxy endpoint wiring can "
            f"be validated. submit output:\n{output}"
        )

        node_agents_payload = _require_payload(
            output,
            lambda item: item.get("check") == "node-agents",
            "Gap G2 node-agent diagnostics",
        )
        proxy_payload = _require_payload(
            output,
            lambda item: item.get("check") == "proxy-env",
            "Gap G2 worker proxy-env diagnostics",
        )

        node_agent_rows = [
            item
            for item in node_agents_payload.get("node_agents_found", [])
            if isinstance(item, dict) and bool(item.get("found"))
        ]
        agent_ports_by_node: dict[str, int] = {}
        for item in node_agent_rows:
            node_id = str(item.get("node_id", ""))
            proxy_port = item.get("proxy_port")
            if node_id and isinstance(proxy_port, int) and proxy_port > 0:
                agent_ports_by_node[node_id] = proxy_port

        worker_env_rows = [
            item
            for item in proxy_payload.get("worker_env", [])
            if isinstance(item, dict) and not item.get("error")
        ]
        assert worker_env_rows, (
            "Gap G2: expected worker environment diagnostics from each node, but none were returned."
        )
        assert agent_ports_by_node, (
            "Gap G2: expected node agents with discoverable proxy ports before checking worker "
            "AWS_ENDPOINT_URL wiring."
        )

        for item in worker_env_rows:
            endpoint = str(item.get("aws_endpoint_url", ""))
            node_id = str(item.get("node_id") or item.get("expected_node_id") or "")
            assert endpoint.startswith("http://127.0.0.1:"), (
                "Gap G2: every worker should have AWS_ENDPOINT_URL set to its local proxy loopback "
                f"endpoint, but worker node_id={node_id!r} reported {endpoint!r}."
            )
            try:
                endpoint_port = urlparse(endpoint).port
            except ValueError:
                endpoint_port = None
            assert isinstance(endpoint_port, int) and endpoint_port > 0, (
                "Gap G2: worker AWS_ENDPOINT_URL should contain a valid proxy port, "
                f"but node_id={node_id!r} reported endpoint {endpoint!r}."
            )
            assert node_id in agent_ports_by_node, (
                "Gap G2: each worker probe should map to a node-agent proxy port for the same node, "
                f"but node_id={node_id!r} was missing from node-agent diagnostics."
            )
            assert endpoint_port == agent_ports_by_node[node_id], (
                "Gap G2: worker AWS_ENDPOINT_URL port should match its node-agent proxy port, "
                f"but node_id={node_id!r} endpoint_port={endpoint_port} "
                f"agent_port={agent_ports_by_node[node_id]}."
            )

    def test_g5_proxy_logs_should_flow_into_fragments_and_db_for_awscli(
        self, roar_project: Path
    ) -> None:
        job_id = f"gap5-{uuid.uuid4().hex[:8]}"
        result = _submit_job(
            roar_project,
            "s3_sdk_matrix.py",
            script_args=["--include-awscli"],
            runtime_env_vars=_manual_gap_runtime_env(job_id),
            tracer="ptrace",
            timeout=420,
        )
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "Gap G5: sdk matrix workload should complete so awscli lineage assertions can run. "
            f"submit output:\n{output}"
        )

        report = _require_payload(
            output,
            lambda item: item.get("script") == "s3_sdk_matrix",
            "Gap G5 sdk matrix report",
        )
        run_id = str(report.get("run_id", ""))
        result_rows = [item for item in report.get("results", []) if isinstance(item, dict)]
        awscli_rows = [item for item in result_rows if str(item.get("method", "")) == "awscli"]
        awscli_paths = {
            str(path)
            for row in awscli_rows
            for path in (row.get("read_path"), row.get("write_path"))
            if isinstance(path, str) and path.startswith("s3://")
        }

        assert run_id, "Gap G5: sdk matrix report should include a non-empty run_id."
        assert awscli_rows and awscli_paths, (
            "Gap G5: sdk matrix with --include-awscli should report awscli read/write paths."
        )

        lineage_rows = _query_s3_lineage_rows(roar_project, f"s3://%/sdk-matrix/{run_id}/%")
        rows_by_path: dict[str, list[dict[str, Any]]] = {}
        for row in lineage_rows:
            path = str(row.get("path", ""))
            rows_by_path.setdefault(path, []).append(row)

        for path in sorted(awscli_paths):
            path_rows = rows_by_path.get(path, [])
            assert path_rows, (
                "Gap G5: awscli S3 operations should be present in DB lineage, "
                f"but path {path!r} was missing from lineage rows."
            )
            assert all(str(row.get("capture_method", "")) == "proxy" for row in path_rows), (
                "Gap G5: awscli paths should be captured via proxy in DB lineage, "
                f"but non-proxy rows were found for path {path!r}: {path_rows}"
            )
            io_kinds = {str(row.get("io_kind", "")) for row in path_rows}
            assert {"read", "write"}.issubset(io_kinds), (
                "Gap G5: awscli path should have both read and write lineage edges, "
                f"but path {path!r} had io kinds {sorted(io_kinds)}."
            )

        session_id, token = _latest_fragment_session(roar_project)
        assert session_id and token, (
            "Gap G5: fragment session key should exist after submit for fragment verification."
        )

        fragment_batches = _fetch_fragment_batches(session_id, token)
        assert fragment_batches, (
            "Gap G5: fragment API should return encrypted batches for this session."
        )

        fragments = _decrypt_fragment_batches(token, fragment_batches)
        assert fragments, (
            "Gap G5: encrypted fragment batches should decrypt into fragment payloads."
        )

        fragment_paths = {
            entry["path"]
            for entry in _s3_fragment_entries(fragments)
            if entry.get("capture_method") == "proxy"
        }
        for path in sorted(awscli_paths):
            assert path in fragment_paths, (
                "Gap G5: awscli S3 operations should be preserved in streamed fragments with "
                f"capture_method=proxy, but path {path!r} was not found in decrypted fragments."
            )


class TestP0HappyPaths:
    def test_hp1_clean_submit_should_produce_proxy_backed_s3_lineage(
        self, roar_project: Path
    ) -> None:
        result = _submit_job(roar_project, "s3_io.py")
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "HP1: clean submit of s3_io.py should complete successfully before lineage checks. "
            f"submit output:\n{output}"
        )

        expected_path = "s3://test-bucket/jobs/s3_io.txt"
        lineage_rows = _query_s3_lineage_rows(roar_project, expected_path)
        assert lineage_rows, (
            "HP1: minimal S3 workload should produce DB lineage rows for the expected S3 path, "
            f"but no rows were found for {expected_path!r}."
        )

        io_kinds = {str(row.get("io_kind", "")) for row in lineage_rows}
        assert {"read", "write"}.issubset(io_kinds), (
            "HP1: minimal workload should include both read and write edges for the S3 object, "
            f"but observed io kinds were {sorted(io_kinds)}."
        )
        assert all(str(row.get("capture_method", "")) == "proxy" for row in lineage_rows), (
            "HP1: all S3 lineage for minimal workload should be proxy-captured, but non-proxy "
            f"capture methods were found: {lineage_rows}"
        )

        write_sizes = [
            int(row.get("size", 0))
            for row in lineage_rows
            if str(row.get("io_kind", "")) == "write"
        ]
        assert write_sizes and all(size > 0 for size in write_sizes), (
            "HP1: non-empty S3 writes should record non-zero sizes, "
            f"but observed write sizes were {write_sizes}."
        )

        all_s3_rows = _query_s3_lineage_rows(roar_project, "s3://%")
        python_rows = [row for row in all_s3_rows if str(row.get("capture_method", "")) == "python"]
        assert not python_rows, (
            "HP1: S3 lineage should not rely on python capture hooks; all rows should be proxy-backed, "
            f"but python-captured S3 rows were found: {python_rows}"
        )

        hash_rows = _query_s3_hash_rows(roar_project, expected_path)
        assert hash_rows, (
            "HP1: S3 lineage should include artifact_hashes rows for the tracked object, "
            f"but no hash rows were found for {expected_path!r}."
        )

        session_id, token = _latest_fragment_session(roar_project)
        assert session_id and token, "HP1: fragment session key should exist after clean submit."

        fragment_batches = _fetch_fragment_batches(session_id, token)
        assert fragment_batches, (
            "HP1: fragment API should return encrypted batches for the job session."
        )

        fragments = _decrypt_fragment_batches(token, fragment_batches)
        fragment_entries = _s3_fragment_entries(fragments)
        proxy_entries = [
            entry
            for entry in fragment_entries
            if entry.get("path") == expected_path and entry.get("capture_method") == "proxy"
        ]
        assert proxy_entries, (
            "HP1: decrypted fragments should include the expected S3 path with capture_method=proxy, "
            f"but no matching entries were found for {expected_path!r}."
        )

    def test_hp2_pipeline_should_produce_proxy_backed_cross_stage_s3_lineage(
        self, roar_project: Path
    ) -> None:
        result = _submit_job(roar_project, "s3_pipeline.py", timeout=420)
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "HP2: clean submit of s3_pipeline.py should complete before end-to-end lineage checks. "
            f"submit output:\n{output}"
        )

        report = _require_payload(
            output,
            lambda item: (
                isinstance(item.get("run_id"), str) and isinstance(item.get("report_key"), str)
            ),
            "HP2 pipeline report",
        )
        run_id = str(report.get("run_id", ""))
        report_key = str(report.get("report_key", ""))
        assert run_id and report_key.startswith("s3://"), (
            "HP2: pipeline output should include a non-empty run_id and S3 report_key, "
            f"but got run_id={run_id!r}, report_key={report_key!r}."
        )

        lineage_rows = _query_s3_lineage_rows(roar_project, f"s3://%/{run_id}/%")
        assert lineage_rows, (
            "HP2: pipeline run should produce S3 lineage rows for the run_id namespace, "
            f"but none were found for run_id={run_id!r}."
        )

        family_markers = {
            "raw": f"/raw/{run_id}/",
            "processed": f"/processed/{run_id}/",
            "models": f"/models/{run_id}/",
            "metrics": f"/metrics/{run_id}/",
            "results": f"/results/{run_id}/",
        }
        for family, marker in family_markers.items():
            family_rows = [row for row in lineage_rows if marker in str(row.get("path", ""))]
            assert family_rows, (
                "HP2: pipeline lineage should include every expected artifact family, "
                f"but no rows were found for family={family!r} marker={marker!r}."
            )

        def _kinds_for(marker: str) -> set[str]:
            return {
                str(row.get("io_kind", ""))
                for row in lineage_rows
                if marker in str(row.get("path", ""))
            }

        assert "read" in _kinds_for(family_markers["raw"]), (
            "HP2: raw inputs should appear as read lineage edges in downstream tasks."
        )
        assert {"read", "write"}.issubset(_kinds_for(family_markers["processed"])), (
            "HP2: processed artifacts should be both written by ingest tasks and read by train tasks."
        )
        assert {"read", "write"}.issubset(_kinds_for(family_markers["models"])), (
            "HP2: model artifacts should be both written by train tasks and read by eval tasks."
        )
        assert "write" in _kinds_for(family_markers["metrics"]), (
            "HP2: metrics artifacts should be written during evaluation."
        )
        assert "write" in _kinds_for(family_markers["results"]), (
            "HP2: final report artifact should be written to S3."
        )

        assert all(str(row.get("capture_method", "")) == "proxy" for row in lineage_rows), (
            "HP2: all S3 lineage rows in the pipeline should be proxy-captured, "
            f"but non-proxy rows were found: {lineage_rows}"
        )

        write_sizes = [
            int(row.get("size", 0))
            for row in lineage_rows
            if str(row.get("io_kind", "")) == "write"
        ]
        assert write_sizes and all(size > 0 for size in write_sizes), (
            "HP2: pipeline S3 writes should record non-zero artifact sizes, "
            f"but observed write sizes were {write_sizes}."
        )

        hash_rows = _query_s3_hash_rows(roar_project, f"s3://%/{run_id}/%")
        assert hash_rows, (
            "HP2: pipeline artifacts should include artifact_hashes rows (ETag/hash metadata), "
            "but none were found for this run_id."
        )

        hashed_paths = {str(row.get("path", "")) for row in hash_rows}
        for family, marker in family_markers.items():
            assert any(marker in path for path in hashed_paths), (
                "HP2: each artifact family should have hash coverage in artifact_hashes, "
                f"but family={family!r} marker={marker!r} was missing from hash rows."
            )

        session_id, token = _latest_fragment_session(roar_project)
        assert session_id and token, "HP2: fragment session key should exist after pipeline submit."

        fragment_batches = _fetch_fragment_batches(session_id, token)
        assert fragment_batches, (
            "HP2: fragment API should return encrypted batches for pipeline run."
        )

        fragments = _decrypt_fragment_batches(token, fragment_batches)
        fragment_entries = _s3_fragment_entries(fragments)
        run_entries = [
            entry for entry in fragment_entries if f"/{run_id}/" in entry.get("path", "")
        ]
        assert run_entries, (
            "HP2: decrypted fragments should include S3 entries for the pipeline run_id, "
            f"but none were found for run_id={run_id!r}."
        )
        assert all(entry.get("capture_method") == "proxy" for entry in run_entries), (
            "HP2: pipeline fragment entries should be proxy-captured, "
            f"but non-proxy entries were found: {run_entries}"
        )
        assert any(
            entry.get("path") == report_key and entry.get("io_kind") == "write"
            for entry in run_entries
        ), (
            "HP2: fragments should include a write entry for the final report artifact, "
            f"but report_key={report_key!r} was not present as a write entry."
        )


class TestP1Gaps:
    def test_g3_submit_rewrite_missing_node_agent_and_wrap_env_injection(
        self, roar_project: Path
    ) -> None:
        result = _submit_job(
            roar_project,
            "roar_diagnostic_probe.py",
            script_args=["--check", "collector"],
        )
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "Gap G3: diagnostic probe should run successfully so submit-time env injection can be "
            f"validated. submit output:\n{output}"
        )

        payload = _require_payload(
            output,
            lambda item: item.get("check") == "collector",
            "Gap G3 driver-env diagnostics",
        )
        driver_env = payload.get("driver_env", {})
        assert isinstance(driver_env, dict), (
            "Gap G3: diagnostic payload should include a driver_env snapshot for submit rewrite checks."
        )

        assert str(driver_env.get("ROAR_JOB_INSTRUMENTED", "")) == "1", (
            "Gap G3: submit rewrite should inject ROAR_JOB_INSTRUMENTED=1 for wrapped Ray jobs, "
            f"but observed value was {driver_env.get('ROAR_JOB_INSTRUMENTED')!r}."
        )
        assert str(driver_env.get("ROAR_RAY_NODE_AGENTS", "")) == "1", (
            "Gap G3: submit rewrite should inject ROAR_RAY_NODE_AGENTS=1 automatically, "
            f"but observed value was {driver_env.get('ROAR_RAY_NODE_AGENTS')!r}."
        )
        assert str(driver_env.get("ROAR_WRAP", "")) == "1", (
            "Gap G3: submit rewrite should inject ROAR_WRAP=1 automatically, "
            f"but observed value was {driver_env.get('ROAR_WRAP')!r}."
        )
        assert str(driver_env.get("GLAAS_URL", "")) != "", (
            "Gap G3: submit rewrite should propagate GLAAS_URL into the driver runtime environment."
        )
        assert str(driver_env.get("ROAR_SESSION_ID", "")) != "", (
            "Gap G3: submit rewrite should inject ROAR_SESSION_ID for fragment streaming."
        )
        assert str(driver_env.get("ROAR_FRAGMENT_TOKEN", "")) != "", (
            "Gap G3: submit rewrite should inject ROAR_FRAGMENT_TOKEN for fragment streaming."
        )

    def test_g4_sdk_matrix_not_fully_proxy_captured(self, roar_project: Path) -> None:
        job_id = f"gap4-{uuid.uuid4().hex[:8]}"
        result = _submit_job(
            roar_project,
            "s3_sdk_matrix.py",
            runtime_env_vars=_manual_gap_runtime_env(job_id),
            tracer="ptrace",
            timeout=360,
        )
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "Gap G4: sdk matrix workload should complete so proxy capture coverage can be validated. "
            f"submit output:\n{output}"
        )

        report = _require_payload(
            output,
            lambda item: item.get("script") == "s3_sdk_matrix",
            "Gap G4 sdk matrix report",
        )
        run_id = str(report.get("run_id", ""))
        results = [item for item in report.get("results", []) if isinstance(item, dict)]

        expected_paths = {
            str(path)
            for item in results
            for path in (item.get("write_path"), item.get("read_path"))
            if isinstance(path, str) and path.startswith("s3://")
        }
        assert run_id and expected_paths, (
            "Gap G4: sdk matrix report should include run_id and expected S3 paths for validation."
        )

        lineage_rows = _query_s3_lineage_rows(roar_project, f"s3://%/sdk-matrix/{run_id}/%")
        lineage_paths = {
            str(row.get("path", "")) for row in lineage_rows if str(row.get("path", ""))
        }
        missing_paths = sorted(expected_paths - lineage_paths)
        assert not missing_paths, (
            "Gap G4: every SDK call-path S3 object should appear in DB lineage, "
            f"but missing paths were: {missing_paths}"
        )

        non_proxy_rows = [
            row
            for row in lineage_rows
            if str(row.get("path", "")) in expected_paths
            and str(row.get("capture_method", "")) != "proxy"
        ]
        assert not non_proxy_rows, (
            "Gap G4: all SDK call-path S3 lineage should be capture_method=proxy, "
            f"but non-proxy rows were found: {non_proxy_rows}"
        )

        hash_rows = _query_s3_hash_rows(roar_project, f"s3://%/sdk-matrix/{run_id}/%")
        hash_paths = {str(row.get("path", "")) for row in hash_rows if str(row.get("path", ""))}
        missing_hash_paths = sorted(expected_paths - hash_paths)
        assert not missing_hash_paths, (
            "Gap G4: every SDK call-path S3 artifact should have artifact_hashes coverage, "
            f"but missing hash rows were: {missing_hash_paths}"
        )


class TestP2Gaps:
    def test_g6_collector_actor_still_created_in_sentinel_path(self, roar_project: Path) -> None:
        result = _submit_job(
            roar_project,
            "roar_diagnostic_probe.py",
            script_args=["--check", "collector"],
        )
        output = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, (
            "Gap G6: diagnostic probe should run successfully so collector actor presence can be "
            f"validated. submit output:\n{output}"
        )

        payload = _require_payload(
            output,
            lambda item: item.get("check") == "collector",
            "Gap G6 collector diagnostics",
        )
        assert bool(payload.get("collector_exists")) is False, (
            "Gap G6: sentinel path should no longer create roar-log-collector actors, "
            "but diagnostics reported collector_exists=True."
        )
