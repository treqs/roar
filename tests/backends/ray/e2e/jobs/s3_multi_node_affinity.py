"""Pin S3 operations to specific nodes and report node->key affinity."""

from __future__ import annotations

import argparse
import contextlib
import json
import time
import uuid
from typing import Any

import boto3

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


def _s3_client():
    return boto3.client("s3")


def _alive_nodes() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for node in ray.nodes():
        if not isinstance(node, dict) or not node.get("Alive"):
            continue
        node_id = _to_text(node.get("NodeID"))
        if not node_id:
            continue
        resources = node.get("Resources", {})
        node_resource = ""
        if isinstance(resources, dict):
            for key in resources:
                key_text = str(key)
                if key_text.startswith("node:"):
                    node_resource = key_text
                    break
        out.append(
            {
                "node_id": node_id,
                "node_ip": _to_text(node.get("NodeManagerAddress")),
                "node_resource": node_resource,
            }
        )
    return out


def _node_id() -> str:
    try:
        return _to_text(ray.get_runtime_context().get_node_id())
    except Exception:
        return ""


@ray.remote
def _node_s3_write(
    run_id: str,
    bucket: str,
    index: int,
    operations_per_node: int,
    sleep_seconds: float,
) -> dict[str, Any]:
    node_id = _node_id()
    s3 = _s3_client()
    paths: list[str] = []
    payload_matches: list[bool] = []
    etags: list[str] = []
    for op_index in range(operations_per_node):
        key = f"multi-node-affinity/{run_id}/{str(node_id)[:8]}/item-{index:03d}-op-{op_index:03d}.txt"
        payload = f"{run_id}|{node_id}|{index}|{op_index}|{time.time_ns()}"
        put_resp = s3.put_object(Bucket=bucket, Key=key, Body=payload.encode("utf-8"))
        get_resp = s3.get_object(Bucket=bucket, Key=key)
        body = get_resp["Body"].read().decode("utf-8")
        paths.append(f"s3://{bucket}/{key}")
        etags.append(str((put_resp or {}).get("ETag", "")))
        payload_matches.append(body == payload)
        if sleep_seconds > 0 and op_index + 1 < operations_per_node:
            time.sleep(sleep_seconds)
    return {
        "node_id": node_id,
        "index": index,
        "bucket": bucket,
        "path": paths[-1],
        "paths": paths,
        "etags": etags,
        "payload_match": all(payload_matches),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="test-bucket")
    parser.add_argument("--operations-per-node", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    ray.init(address="auto")
    report: dict[str, Any] = {
        "script": "s3_multi_node_affinity",
        "run_id": run_id,
        "bucket": args.bucket,
        "results": [],
        "errors": [],
    }
    try:
        nodes = _alive_nodes()
        report["alive_nodes"] = nodes

        scheduled: list[ray.ObjectRef] = []
        for index, node in enumerate(nodes):
            node_resource = str(node.get("node_resource", ""))
            options: dict[str, Any] = {}
            if node_resource:
                options["resources"] = {node_resource: 0.001}
            scheduled.append(
                _node_s3_write.options(**options).remote(
                    run_id,
                    str(args.bucket),
                    index,
                    int(args.operations_per_node),
                    float(args.sleep_seconds),
                )
            )

        for ref in scheduled:
            try:
                report["results"].append(ray.get(ref, timeout=120))
            except Exception as exc:
                report["errors"].append({"error": str(exc)})

        node_to_keys: dict[str, list[str]] = {}
        for item in report["results"]:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or "")
            raw_paths = item.get("paths")
            if isinstance(raw_paths, list):
                paths = [str(path) for path in raw_paths if str(path)]
            else:
                path = str(item.get("path") or "")
                paths = [path] if path else []
            if not node_id or not paths:
                continue
            node_to_keys.setdefault(node_id, []).extend(paths)
        report["node_to_keys"] = node_to_keys

        print(json.dumps(report, sort_keys=True))
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()

    if report["errors"]:
        return 1
    if any(
        not bool(item.get("payload_match")) for item in report["results"] if isinstance(item, dict)
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
