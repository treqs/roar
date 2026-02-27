"""
TDD: roar captures I/O from workers on remote nodes, not just the driver.

These tests verify that roar's per-node agent correctly instruments workers
on ray-worker-1 and ray-worker-2 (separate Docker containers from ray-head).

They FAIL until the per-node agent is implemented and log collection works.

Run against a live cluster:
    pytest tests/e2e/ray/test_multi_node_capture.py -v --timeout=180
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.e2e.ray.conftest import submit_job_on_head
from tests.e2e.ray.test_file_io_capture import _query_roar_db

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"


def _get_worker_container_ips(compose_file: Path) -> dict[str, str]:
    """Return {container_name: ip} for the two worker containers."""
    ips = {}
    for service in ("ray-worker-1", "ray-worker-2"):
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "exec",
                "-T",
                service,
                "hostname",
                "-i",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            ips[service] = result.stdout.strip()
    return ips


class TestMultiNodeCapture:
    """roar captures I/O from workers running on remote Docker containers."""

    def test_io_captured_from_worker_containers(self, ray_cluster):
        """
        The lineage DB should contain artifacts from I/O that happened
        inside ray-worker-1 or ray-worker-2 containers — not just the driver.

        We verify this by checking that captured artifact metadata includes
        node IDs corresponding to the worker containers.

        FAILS until roar's per-node agent ships logs from workers back to the driver.
        """
        stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/attributed_file_io.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        # Parse job output to get which node_ids the tasks ran on
        result_json = None
        for line in stdout.splitlines():
            try:
                result_json = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

        assert result_json is not None, f"Could not parse job output: {stdout}"

        worker_node_ids = {r["node_id"] for r in result_json["writes"]}
        assert len(worker_node_ids) >= 2, (
            f"Tasks only ran on {len(worker_node_ids)} node(s): {worker_node_ids}. "
            "Need tasks on at least 2 nodes to test multi-node capture."
        )

        # Check that the roar DB contains artifacts from at least 2 distinct nodes
        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT DISTINCT json_extract(metadata, '$.ray_node_id') as node_id "
            "FROM artifacts "
            "WHERE path LIKE '%attributed%' AND metadata IS NOT NULL",
        )
        captured_node_ids = {r["node_id"] for r in rows if r["node_id"]}
        assert len(captured_node_ids) >= 2, (
            f"Expected artifacts from ≥ 2 Ray nodes, got {len(captured_node_ids)}: "
            f"{captured_node_ids}. "
            "roar's per-node agent is not collecting I/O from remote worker containers."
        )

    def test_worker_logs_merged_into_single_lineage_record(self, ray_cluster):
        """
        Even though I/O happens on multiple nodes, roar should produce a single
        unified job record in the local DB with all artifacts from all nodes.

        FAILS until multi-node log merging is implemented in the driver.
        """
        submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/attributed_file_io.py",
            env={"ROAR_WRAP": "1"},
        )

        # Should be exactly 1 job record
        job_rows = _query_roar_db(COMPOSE_FILE, "SELECT id FROM jobs")
        assert len(job_rows) == 1, (
            f"Expected 1 unified job record, got {len(job_rows)}. "
            "Multi-node logs should merge into a single job."
        )

        # That job should reference artifacts from multiple nodes
        artifact_rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT COUNT(*) as cnt FROM artifacts WHERE path LIKE '%attributed%'",
        )
        count = artifact_rows[0]["cnt"] if artifact_rows else 0
        assert count >= 6, (
            f"Expected ≥ 6 artifacts in the unified job record, got {count}. "
            "Worker-node artifacts are not being merged into the driver's job record."
        )

    def test_native_tracer_captures_non_python_io(self, ray_cluster):
        """
        Ray Data uses Arrow C++ under the hood — it bypasses Python's open().
        The native tracer (eBPF/preload) on each worker node should capture this
        at the syscall level.

        FAILS until native tracers are running on remote worker nodes.
        """
        # Ray Data job writing parquet (Arrow, not Python open())
        _stdout, stderr, returncode = submit_job_on_head(
            COMPOSE_FILE,
            f"{JOBS_DIR}/pipeline.py",
            env={"ROAR_WRAP": "1"},
        )
        assert returncode == 0, f"Job failed:\n{stderr}"

        # Parquet files written by Arrow bypass Python's open(),
        # so they'll only appear if the native tracer is running on the worker.
        rows = _query_roar_db(
            COMPOSE_FILE,
            "SELECT path, capture_method FROM artifacts "
            "WHERE path LIKE '%.parquet' AND capture_method = 'tracer'",
        )
        assert len(rows) >= 1, (
            "Expected parquet output to be captured by the native tracer on the worker node. "
            "No tracer-captured parquet artifacts found. "
            "roar's native tracer is not running on remote worker containers."
        )
