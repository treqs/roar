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
def _node_s3_write(run_id: str, bucket: str, index: int) -> dict[str, Any]:
    node_id = _node_id()
    key = f"multi-node-affinity/{run_id}/{str(node_id)[:8]}/item-{index:03d}.txt"
    payload = f"{run_id}|{node_id}|{index}|{time.time_ns()}"
    s3 = _s3_client()
    put_resp = s3.put_object(Bucket=bucket, Key=key, Body=payload.encode("utf-8"))
    get_resp = s3.get_object(Bucket=bucket, Key=key)
    body = get_resp["Body"].read().decode("utf-8")
    return {
        "node_id": node_id,
        "index": index,
        "bucket": bucket,
        "key": key,
        "path": f"s3://{bucket}/{key}",
        "etag": str((put_resp or {}).get("ETag", "")),
        "payload_match": body == payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="test-bucket")
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
                _node_s3_write.options(**options).remote(run_id, str(args.bucket), index)
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
            path = str(item.get("path") or "")
            if not node_id or not path:
                continue
            node_to_keys.setdefault(node_id, []).append(path)
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
