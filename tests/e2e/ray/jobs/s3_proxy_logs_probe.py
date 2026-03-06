"""Submit a Ray job that exercises S3 proxy-log collection via the setup hook."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import boto3

from roar.ray._agent_names import build_node_agent_name
from roar.services.execution.inject.sitecustomize import _collect_ray_io

_ORIGINAL_ROAR_WRAP = os.environ.get("ROAR_WRAP")
os.environ["ROAR_WRAP"] = "0"
import ray
from ray.job_submission import JobStatus, JobSubmissionClient
from roar.ray.node_agent import RoarNodeAgent
from roar.ray.roar_worker import _parse_proxy_log_lines

if _ORIGINAL_ROAR_WRAP is None:
    os.environ.pop("ROAR_WRAP", None)
else:
    os.environ["ROAR_WRAP"] = _ORIGINAL_ROAR_WRAP

_DASHBOARD_URL = "http://127.0.0.1:8265"
_ENTRYPOINT = "python /app/tests/e2e/ray/jobs/s3_proxy_logs_probe.py --run-workload"
_DB_PATH = Path("/app/.roar/roar.db")
_POLL_INTERVAL_SECONDS = 1.0
_TIMEOUT_SECONDS = 120.0
_NODE_AGENT_TIMEOUT_SECONDS = 45.0
_NODE_AGENT_RESOURCE_FRACTION = 0.0001
_TEST_BUCKET = "test-bucket"
_TEST_DATA = "hello from proxy test"
_TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.STOPPED,
}


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.hex()
        except Exception:
            return value.decode("utf-8", errors="ignore")
    return str(value)


def _prime_proxy(label: str) -> None:
    marker_path = f"/tmp/roar-s3-proxy-prime-{label}.txt"
    with open(marker_path, "w", encoding="utf-8") as handle:
        handle.write(label)


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name="us-east-1",
    )


@ray.remote(max_retries=0)
def s3_write(bucket: str, key: str, data: str) -> str:
    _prime_proxy("write")
    s3 = _s3_client()
    s3.put_object(Bucket=bucket, Key=key, Body=data.encode("utf-8"))
    return f"s3://{bucket}/{key}"


@ray.remote(max_retries=0)
def s3_read(bucket: str, key: str) -> str:
    _prime_proxy("read")
    s3 = _s3_client()
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def _run_workload(run_id: str) -> None:
    ray.init(address="auto", ignore_reinit_error=True, logging_level="ERROR")
    try:
        key = f"proxy-test/{run_id}/data.txt"
        write_uri = ray.get(s3_write.remote(_TEST_BUCKET, key, _TEST_DATA))
        result = ray.get(s3_read.remote(_TEST_BUCKET, key))
        print(
            json.dumps(
                {
                    "artifacts_expected": 2,
                    "bucket": _TEST_BUCKET,
                    "data": result,
                    "key": key,
                    "status": "ok",
                    "write_uri": write_uri,
                },
                sort_keys=True,
            )
        )
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()


def _wait_for_terminal_status(client: JobSubmissionClient, job_id: str) -> JobStatus:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    last_status: JobStatus | None = None

    while time.monotonic() < deadline:
        status = client.get_job_status(job_id)
        last_status = status
        if status in _TERMINAL_JOB_STATUSES:
            return status
        time.sleep(_POLL_INTERVAL_SECONDS)

    last_status_name = last_status.name if isinstance(last_status, JobStatus) else str(last_status)
    raise TimeoutError(f"Timed out waiting for Ray job {job_id}; last status={last_status_name}")


def _build_payload(client: JobSubmissionClient, job_id: str, status: JobStatus) -> dict[str, Any]:
    info = client.get_job_info(job_id)
    logs = client.get_job_logs(job_id)
    return {
        "driver_exit_code": getattr(info, "driver_exit_code", None),
        "entrypoint": getattr(info, "entrypoint", ""),
        "error_type": getattr(info, "error_type", ""),
        "job_id": job_id,
        "logs": logs,
        "message": getattr(info, "message", ""),
        "status": status.name,
    }


def _build_runtime_env(probe_job_id: str) -> dict[str, Any]:
    return {
        "worker_process_setup_hook": "roar.ray.roar_worker._startup",
        "env_vars": {
            "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ENDPOINT_URL": os.environ.get("AWS_ENDPOINT_URL", "http://minio:9000"),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
            "ROAR_JOB_ID": probe_job_id,
            "ROAR_JOB_INSTRUMENTED": "1",
            "ROAR_RAY_NODE_AGENTS": "1",
            "ROAR_WRAP": "1",
        },
    }


def _alive_node_ids() -> list[str]:
    node_ids: list[str] = []
    for node in ray.nodes():
        if not isinstance(node, dict) or not node.get("Alive"):
            continue
        node_id = _to_text(node.get("NodeID"))
        if node_id:
            node_ids.append(node_id)
    return node_ids


def _node_resource_key(node: dict[str, Any]) -> str:
    resources = node.get("Resources", {})
    if not isinstance(resources, dict):
        return ""
    for key in resources:
        key_text = str(key)
        if key_text.startswith("node:"):
            return key_text
    return ""


def _spawn_node_agents(job_id: str) -> dict[str, dict[str, Any]]:
    node_agents: dict[str, dict[str, Any]] = {}
    for node in ray.nodes():
        if not isinstance(node, dict) or not node.get("Alive"):
            continue
        node_id = _to_text(node.get("NodeID"))
        if not node_id:
            continue

        actor_name = build_node_agent_name(job_id, node_id)
        actor = None
        try:
            actor = ray.get_actor(actor_name, namespace="roar")
        except Exception:
            try:
                options: dict[str, Any] = {
                    "name": actor_name,
                    "namespace": "roar",
                    "lifetime": "detached",
                    "num_cpus": 0,
                }
                node_resource = _node_resource_key(node)
                if node_resource:
                    options["resources"] = {node_resource: _NODE_AGENT_RESOURCE_FRACTION}
                actor = RoarNodeAgent.options(**options).remote(job_id=job_id)
            except ValueError:
                # Race: actor was created between get_actor and create
                actor = ray.get_actor(actor_name, namespace="roar")

        node_agents[node_id] = {
            "actor": actor,
            "name": actor_name,
        }
    return node_agents


def _wait_for_node_agents(node_agents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + _NODE_AGENT_TIMEOUT_SECONDS
    last_seen: dict[str, dict[str, Any]] = {}

    while time.monotonic() < deadline:
        ready_agents: dict[str, dict[str, Any]] = {}
        for node_id, info in node_agents.items():
            actor_name = str(info.get("name") or "")
            actor = info.get("actor")
            if actor is None:
                continue
            try:
                proxy_port = ray.get(actor.get_proxy_port.remote(), timeout=5)
            except Exception:
                continue
            if isinstance(proxy_port, int) and proxy_port > 0:
                ready_agents[node_id] = {
                    "name": actor_name,
                    "actor": actor,
                    "proxy_port": proxy_port,
                }
        last_seen = ready_agents
        if node_agents and len(ready_agents) >= len(node_agents):
            return ready_agents
        time.sleep(0.25)

    raise TimeoutError(
        f"Timed out waiting for node agents; ready={json.dumps(last_seen, sort_keys=True)}"
    )


def _latest_json_dict(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _summarize_proxy_logs(proxy_logs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    proxy_log_lines: list[str] = []
    by_node: dict[str, int] = {}
    for node_id, payload in proxy_logs.items():
        lines = payload.get("proxy_log_lines", [])
        if not isinstance(lines, list):
            continue
        normalized_lines = [str(line) for line in lines]
        proxy_log_lines.extend(normalized_lines)
        by_node[str(node_id)] = len(normalized_lines)

    parsed_entries = _parse_proxy_log_lines(proxy_log_lines)
    s3_paths = sorted({ref.path for _kind, ref in parsed_entries})
    return {
        "node_agent_count": len(proxy_logs),
        "proxy_log_line_count": len(proxy_log_lines),
        "proxy_log_lines_by_node": by_node,
        "proxy_ready_count": sum(1 for line in proxy_log_lines if line.startswith("ROAR_PROXY_READY")),
        "proxy_s3_event_count": len(parsed_entries),
        "proxy_s3_paths": s3_paths,
    }


def _collect_proxy_logs(node_agents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    proxy_logs: dict[str, dict[str, Any]] = {}
    for node_id, info in node_agents.items():
        actor = info.get("actor")
        if actor is None:
            continue
        try:
            payload = ray.get(actor.collect_logs.remote(), timeout=15)
            if isinstance(payload, dict):
                payload.setdefault("node_id", node_id)
                proxy_logs[node_id] = payload
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                ray.get(actor.shutdown.remote(), timeout=5)
            with contextlib.suppress(Exception):
                ray.kill(actor)
    return proxy_logs


def _query_db(path_like: str) -> dict[str, Any]:
    if not _DB_PATH.exists():
        return {
            "db_exists": False,
            "db_job_input_count": 0,
            "db_job_output_count": 0,
            "db_s3_artifact_count": 0,
            "db_s3_rows": [],
        }

    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        artifact_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT first_seen_path, source_type, capture_method, size, hash
                FROM artifacts
                WHERE first_seen_path LIKE ?
                ORDER BY first_seen_at DESC
                """,
                (path_like,),
            ).fetchall()
        ]
        input_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM job_inputs WHERE path LIKE ?",
                (path_like,),
            ).fetchone()[0]
        )
        output_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM job_outputs WHERE path LIKE ?",
                (path_like,),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    return {
        "db_exists": True,
        "db_job_input_count": input_count,
        "db_job_output_count": output_count,
        "db_s3_artifact_count": len(artifact_rows),
        "db_s3_rows": artifact_rows,
    }


def _submit_probe_job() -> int:
    probe_job_id = f"proxy-logs-probe-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4().hex[:8]
    key = f"proxy-test/{run_id}/data.txt"
    os.environ["ROAR_JOB_ID"] = probe_job_id

    ray.init(address="auto", ignore_reinit_error=True, logging_level="ERROR")
    try:
        node_agents = _wait_for_node_agents(_spawn_node_agents(probe_job_id))

        client = JobSubmissionClient(_DASHBOARD_URL)
        job_id = client.submit_job(
            entrypoint=f"{_ENTRYPOINT} --run-id {run_id}",
            runtime_env=_build_runtime_env(probe_job_id),
        )
        status = _wait_for_terminal_status(client, job_id)
        payload = _build_payload(client, job_id, status)
        workload_payload = _latest_json_dict(str(payload.get("logs") or ""))

        proxy_logs = _collect_proxy_logs(node_agents)
        proxy_summary = _summarize_proxy_logs(proxy_logs)

        _collect_ray_io(proxy_logs=proxy_logs)
        db_summary = _query_db(f"s3://{_TEST_BUCKET}/proxy-test/{run_id}/%")

        payload.update(proxy_summary)
        payload.update(db_summary)
        payload["expected_s3_path"] = f"s3://{_TEST_BUCKET}/{key}"
        payload["node_agents"] = node_agents
        payload["probe_job_id"] = probe_job_id
        payload["run_id"] = run_id
        payload["workload"] = workload_payload
        print(json.dumps(payload, sort_keys=True))
        return 0
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="", help="Run id for the submitted S3 workload.")
    parser.add_argument(
        "--run-workload",
        action="store_true",
        help="Run as the submitted Ray job entrypoint instead of submitting the job.",
    )
    args = parser.parse_args(argv)

    if args.run_workload:
        run_id = args.run_id or uuid.uuid4().hex[:8]
        _run_workload(run_id)
        return 0

    return _submit_probe_job()


if __name__ == "__main__":
    raise SystemExit(main())
