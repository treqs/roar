"""High-throughput S3 probe with configurable operations and parallelism."""

from __future__ import annotations

import argparse
import json
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


@ray.remote
def _blast(worker_index: int, run_id: str, bucket: str, ops: int) -> dict[str, Any]:
    client = _s3_client()
    node_id = _node_id()
    success = 0
    for offset in range(max(0, ops)):
        op_id = (worker_index * 1_000_000) + offset
        key = f"high-throughput/{run_id}/w{worker_index:03d}/op-{op_id:09d}.txt"
        payload = f"{run_id}|{worker_index}|{offset}|{time.time_ns()}"
        client.put_object(Bucket=bucket, Key=key, Body=payload.encode("utf-8"))
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        if body != payload:
            raise RuntimeError(f"payload mismatch: {key}")
        success += 1

    return {
        "worker_index": worker_index,
        "node_id": node_id,
        "ops_requested": ops,
        "ops_succeeded": success,
    }


def _distribute_ops(total_ops: int, workers: int) -> list[int]:
    workers = max(1, workers)
    base = total_ops // workers
    rem = total_ops % workers
    return [base + (1 if idx < rem else 0) for idx in range(workers)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ops", type=int, default=5000)
    parser.add_argument("--parallelism", type=int, default=64)
    parser.add_argument("--bucket", default="test-bucket")
    args = parser.parse_args(argv)

    total_ops = max(1, int(args.ops))
    parallelism = max(1, int(args.parallelism))
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"

    ray.init(address="auto")
    report: dict[str, Any] = {
        "script": "s3_high_throughput",
        "run_id": run_id,
        "bucket": args.bucket,
        "ops": total_ops,
        "parallelism": parallelism,
        "worker_results": [],
        "errors": [],
    }
    started = time.perf_counter()
    try:
        ops_per_worker = _distribute_ops(total_ops, parallelism)
        refs: list[tuple[int, ray.ObjectRef]] = []
        for worker_index, worker_ops in enumerate(ops_per_worker):
            if worker_ops <= 0:
                continue
            refs.append(
                (
                    worker_index,
                    _blast.remote(worker_index, run_id, str(args.bucket), worker_ops),
                )
            )

        for worker_index, ref in refs:
            try:
                report["worker_results"].append(ray.get(ref, timeout=600))
            except Exception as exc:
                report["errors"].append({"worker_index": worker_index, "error": str(exc)})

        duration_s = time.perf_counter() - started
        total_succeeded = sum(
            int(item.get("ops_succeeded", 0))
            for item in report["worker_results"]
            if isinstance(item, dict)
        )
        report["summary"] = {
            "duration_s": duration_s,
            "ops_succeeded": total_succeeded,
            "ops_failed": max(0, total_ops - total_succeeded),
            "throughput_ops_per_s": (float(total_succeeded) / duration_s) if duration_s > 0 else 0.0,
        }
        print(json.dumps(report, sort_keys=True))
    finally:
        try:
            ray.shutdown()
        except Exception:
            pass

    if report["errors"]:
        return 1
    return 0 if report.get("summary", {}).get("ops_succeeded", 0) == total_ops else 1


if __name__ == "__main__":
    raise SystemExit(main())
