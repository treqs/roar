"""Consolidated Ray diagnostics for node agents, proxy env, collector, and binary."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import platform
import shutil
import subprocess
import sys
from typing import Any

import ray


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.hex()
        except Exception:
            return value.decode("utf-8", errors="ignore")
    return str(value)


def _current_node_id() -> str:
    try:
        return _to_text(ray.get_runtime_context().get_node_id())
    except Exception:
        return ""


def _node_resource_key(node: dict[str, Any]) -> str:
    resources = node.get("Resources", {})
    if not isinstance(resources, dict):
        return ""
    for key in resources:
        key_text = str(key)
        if key_text.startswith("node:"):
            return key_text
    return ""


def _alive_nodes() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for node in ray.nodes():
        if not isinstance(node, dict) or not node.get("Alive"):
            continue
        node_id = _to_text(node.get("NodeID"))
        if not node_id:
            continue
        out.append(
            {
                "node_id": node_id,
                "node_ip": _to_text(node.get("NodeManagerAddress")),
                "node_resource": _node_resource_key(node),
            }
        )
    return out


def _list_actors() -> list[dict[str, str]]:
    try:
        from ray.util import state

        actors = state.list_actors(detail=True)
    except Exception:
        return []

    out: list[dict[str, str]] = []
    for actor in actors:
        if not isinstance(actor, dict):
            continue
        out.append(
            {
                "name": _to_text(actor.get("name")),
                "state": _to_text(actor.get("state")),
                "class_name": _to_text(actor.get("class_name")),
            }
        )
    return out


@ray.remote(num_cpus=0)
def _worker_probe(check_binary: bool = False) -> dict[str, Any]:
    binary_path = shutil.which("roar-proxy")
    start_ok = False
    start_code: int | None = None
    if check_binary and binary_path:
        try:
            result = subprocess.run(
                [binary_path, "--help"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            start_code = int(result.returncode)
            start_ok = result.returncode == 0
        except Exception:
            start_ok = False
            start_code = None

    return {
        "node_id": _current_node_id(),
        "aws_endpoint_url": os.getenv("AWS_ENDPOINT_URL", ""),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "proxy_binary_path": binary_path or "",
        "proxy_binary_found": bool(binary_path),
        "proxy_start_ok": start_ok,
        "proxy_start_code": start_code,
    }


def _run_per_node_worker_probe(check_binary: bool) -> list[dict[str, Any]]:
    nodes = _alive_nodes()
    scheduled: list[tuple[str, ray.ObjectRef]] = []
    for node in nodes:
        options: dict[str, Any] = {"num_cpus": 0}
        node_resource = node.get("node_resource", "")
        if isinstance(node_resource, str) and node_resource:
            options["resources"] = {node_resource: 0.001}
        ref = _worker_probe.options(**options).remote(check_binary=check_binary)
        scheduled.append((str(node.get("node_id", "")), ref))

    out: list[dict[str, Any]] = []
    for expected_node_id, ref in scheduled:
        try:
            payload = ray.get(ref, timeout=30)
            if isinstance(payload, dict):
                payload.setdefault("expected_node_id", expected_node_id)
                out.append(payload)
            else:
                out.append({"expected_node_id": expected_node_id, "error": "non-dict payload"})
        except Exception as exc:
            out.append({"expected_node_id": expected_node_id, "error": str(exc)})
    return out


def _agent_name(job_id: str, node_id: str) -> str:
    return f"roar-node-agent-{job_id}-{str(node_id)[:8]}"


def _collect_agent_payload(actor_name: str) -> dict[str, Any]:
    try:
        actor = ray.get_actor(actor_name, namespace="roar")
    except Exception:
        return {"name": actor_name, "found": False}

    payload: dict[str, Any] = {"name": actor_name, "found": True}
    try:
        result = ray.get(actor.collect_logs.remote(), timeout=5)
        if isinstance(result, dict):
            lines = result.get("proxy_log_lines", [])
            payload.update(result)
            payload["proxy_log_line_count"] = len(lines) if isinstance(lines, list) else 0
        else:
            payload["collect_logs_payload"] = _to_text(result)
    except Exception as exc:
        payload["collect_logs_error"] = str(exc)
    return payload


def _check_node_agents(job_id: str) -> dict[str, Any]:
    nodes = _alive_nodes()
    expected = [_agent_name(job_id, node["node_id"]) for node in nodes]
    found = [_collect_agent_payload(name) for name in expected]
    missing = [item["name"] for item in found if not item.get("found")]
    return {
        "check": "node-agents",
        "job_id": job_id,
        "alive_nodes": nodes,
        "expected_agent_names": expected,
        "node_agents_found": found,
        "node_agents_found_count": len([item for item in found if item.get("found")]),
        "missing_agent_names": missing,
        "actors": _list_actors(),
    }


def _check_proxy_env(job_id: str) -> dict[str, Any]:
    worker_env = _run_per_node_worker_probe(check_binary=False)
    return {
        "check": "proxy-env",
        "job_id": job_id,
        "alive_nodes": _alive_nodes(),
        "worker_env": worker_env,
        "actors": _list_actors(),
    }


def _source_contains(module_name: str, attribute_name: str, needle: str) -> tuple[bool, str]:
    try:
        module = __import__(module_name, fromlist=[attribute_name])
        target = getattr(module, attribute_name)
        source = inspect.getsource(target)
    except Exception as exc:
        return False, str(exc)
    return needle in source, ""


def _proxy_log_plumbing_status() -> dict[str, Any]:
    collect_ray_io_drops_proxy_logs, collect_ray_io_error = _source_contains(
        "roar.services.execution.inject.sitecustomize",
        "_collect_ray_io",
        "del proxy_logs",
    )
    collector_collect_drops_proxy_logs, collector_collect_error = _source_contains(
        "roar.ray.collector",
        "collect",
        "del log_dir, proxy_logs",
    )
    return {
        "collect_ray_io_drops_proxy_logs": collect_ray_io_drops_proxy_logs,
        "collect_ray_io_source_error": collect_ray_io_error,
        "collector_collect_drops_proxy_logs": collector_collect_drops_proxy_logs,
        "collector_collect_source_error": collector_collect_error,
    }


def _check_collector(job_id: str) -> dict[str, Any]:
    actor_name = f"roar-log-collector-{job_id}"
    exists = False
    ping_ok = False
    error = ""
    try:
        actor = ray.get_actor(actor_name, namespace="roar")
        exists = True
        try:
            ping_ok = bool(ray.get(actor.ping.remote(), timeout=5))
        except Exception as exc:
            error = str(exc)
    except Exception as exc:
        error = str(exc)

    return {
        "check": "collector",
        "job_id": job_id,
        "collector_actor_name": actor_name,
        "collector_exists": exists,
        "collector_ping_ok": ping_ok,
        "collector_error": error,
        "proxy_log_plumbing": _proxy_log_plumbing_status(),
        "actors": _list_actors(),
    }


def _check_binary(job_id: str) -> dict[str, Any]:
    worker_binary = _run_per_node_worker_probe(check_binary=True)
    return {
        "check": "binary",
        "job_id": job_id,
        "alive_nodes": _alive_nodes(),
        "worker_binary": worker_binary,
        "actors": _list_actors(),
    }


def _driver_env_snapshot() -> dict[str, str]:
    keys = [
        "ROAR_JOB_INSTRUMENTED",
        "ROAR_RAY_NODE_AGENTS",
        "ROAR_WRAP",
        "GLAAS_URL",
        "GLAAS_API_URL",
        "ROAR_SESSION_ID",
        "ROAR_FRAGMENT_TOKEN",
        "ROAR_JOB_ID",
        "AWS_ENDPOINT_URL",
    ]
    return {key: os.getenv(key, "") for key in keys}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        default="node-agents,proxy-env,collector,binary",
        help="Comma-separated checks: node-agents,proxy-env,collector,binary",
    )
    args = parser.parse_args(argv)
    checks = [item.strip() for item in str(args.check).split(",") if item.strip()]
    valid = {"node-agents", "proxy-env", "collector", "binary"}
    invalid = sorted(set(checks) - valid)
    if invalid:
        print(json.dumps({"error": "invalid check values", "invalid": invalid}, sort_keys=True))
        return 2

    ray.init(address="auto", ignore_reinit_error=True, logging_level="ERROR")
    try:
        job_id = os.getenv("ROAR_JOB_ID") or os.getenv("RAY_JOB_ID") or "default"
        runners = {
            "node-agents": _check_node_agents,
            "proxy-env": _check_proxy_env,
            "collector": _check_collector,
            "binary": _check_binary,
        }
        for check in checks:
            payload = runners[check](str(job_id))
            payload["driver_env"] = _driver_env_snapshot()
            print(json.dumps(payload, sort_keys=True))
    finally:
        try:
            ray.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
