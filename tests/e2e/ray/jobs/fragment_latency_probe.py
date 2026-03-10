"""Emit S3-backed fragment-like events with timestamps and report latency stats."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import time
import uuid
from typing import Any

import boto3

import ray


def _s3_client():
    return boto3.client("s3")


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = math.ceil((pct / 100.0) * len(ordered)) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return float(ordered[idx])


def _node_id() -> str:
    try:
        value = ray.get_runtime_context().get_node_id()
        if isinstance(value, bytes):
            return value.hex()
        return str(value)
    except Exception:
        return ""


@ray.remote
def _emit(index: int, run_id: str, bucket: str) -> dict[str, Any]:
    client = _s3_client()
    emitted_at_ns = time.time_ns()
    key = f"fragment-latency/{run_id}/f{index:05d}-{emitted_at_ns}.json"
    body = json.dumps({"index": index, "emitted_at_ns": emitted_at_ns}).encode("utf-8")

    client.put_object(Bucket=bucket, Key=key, Body=body)
    response = client.get_object(Bucket=bucket, Key=key)
    raw = response["Body"].read().decode("utf-8")
    completed_at_ns = time.time_ns()
    parsed = json.loads(raw)

    return {
        "index": index,
        "node_id": _node_id(),
        "path": f"s3://{bucket}/{key}",
        "emitted_at_ns": emitted_at_ns,
        "completed_at_ns": completed_at_ns,
        "payload_emitted_at_ns": int(parsed["emitted_at_ns"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fragments", type=int, default=200)
    parser.add_argument("--bucket", default="test-bucket")
    args = parser.parse_args(argv)

    total = max(1, int(args.fragments))
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    ray.init(address="auto")
    report: dict[str, Any] = {
        "script": "fragment_latency_probe",
        "run_id": run_id,
        "bucket": args.bucket,
        "fragments_requested": total,
        "records": [],
        "errors": [],
    }
    try:
        refs = [_emit.remote(index, run_id, str(args.bucket)) for index in range(total)]
        for ref in refs:
            try:
                report["records"].append(ray.get(ref, timeout=120))
            except Exception as exc:
                report["errors"].append(str(exc))

        latencies_ms: list[float] = []
        for item in report["records"]:
            if not isinstance(item, dict):
                continue
            emitted = int(item.get("emitted_at_ns", 0))
            completed = int(item.get("completed_at_ns", emitted))
            latencies_ms.append(max(0.0, (completed - emitted) / 1_000_000.0))

        report["latency_ms"] = {
            "count": len(latencies_ms),
            "p50": _percentile(latencies_ms, 50),
            "p95": _percentile(latencies_ms, 95),
            "min": min(latencies_ms) if latencies_ms else 0.0,
            "max": max(latencies_ms) if latencies_ms else 0.0,
        }
        print(json.dumps(report, sort_keys=True))
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()

    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
