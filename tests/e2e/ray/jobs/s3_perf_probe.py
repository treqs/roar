"""Micro-benchmark S3 latency and report p50/p95 metrics as JSON."""

from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from typing import Any

import boto3
import ray


def _s3_client():
    return boto3.client("s3")


def _node_id() -> str:
    try:
        value = ray.get_runtime_context().get_node_id()
        if isinstance(value, bytes):
            return value.hex()
        return str(value)
    except Exception:
        return ""


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = int(math.ceil((percentile / 100.0) * len(ordered))) - 1
    rank = max(0, min(rank, len(ordered) - 1))
    return float(ordered[rank])


@ray.remote
def _run_micro_probe(ops: int, bucket: str, run_id: str) -> dict[str, Any]:
    s3 = _s3_client()
    put_latencies_ms: list[float] = []
    get_latencies_ms: list[float] = []

    for idx in range(ops):
        key = f"s3-perf/{run_id}/item-{idx:05d}.txt"
        payload = f"{run_id}-{idx}-{time.time_ns()}".encode("utf-8")

        start = time.perf_counter()
        s3.put_object(Bucket=bucket, Key=key, Body=payload)
        put_latencies_ms.append((time.perf_counter() - start) * 1000.0)

        start = time.perf_counter()
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        get_latencies_ms.append((time.perf_counter() - start) * 1000.0)
        if body != payload:
            raise RuntimeError(f"payload mismatch at {key}")

    return {
        "node_id": _node_id(),
        "ops": ops,
        "put_latencies_ms": put_latencies_ms,
        "get_latencies_ms": get_latencies_ms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="micro", choices=["micro"])
    parser.add_argument("--ops", type=int, default=100)
    parser.add_argument("--bucket", default="test-bucket")
    args = parser.parse_args(argv)

    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    ray.init(address="auto")
    report: dict[str, Any] = {
        "script": "s3_perf_probe",
        "mode": args.mode,
        "ops": args.ops,
        "bucket": args.bucket,
        "run_id": run_id,
    }
    try:
        payload = ray.get(_run_micro_probe.remote(max(1, int(args.ops)), str(args.bucket), run_id))
        put_latencies = [float(item) for item in payload.get("put_latencies_ms", [])]
        get_latencies = [float(item) for item in payload.get("get_latencies_ms", [])]
        report["node_id"] = payload.get("node_id")
        report["operation_stats"] = {
            "put_object": {
                "count": len(put_latencies),
                "p50_ms": _percentile(put_latencies, 50),
                "p95_ms": _percentile(put_latencies, 95),
                "min_ms": min(put_latencies) if put_latencies else 0.0,
                "max_ms": max(put_latencies) if put_latencies else 0.0,
            },
            "get_object": {
                "count": len(get_latencies),
                "p50_ms": _percentile(get_latencies, 50),
                "p95_ms": _percentile(get_latencies, 95),
                "min_ms": min(get_latencies) if get_latencies else 0.0,
                "max_ms": max(get_latencies) if get_latencies else 0.0,
            },
        }
        print(json.dumps(report, sort_keys=True))
    finally:
        try:
            ray.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
