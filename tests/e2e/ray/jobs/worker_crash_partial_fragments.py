"""Run mixed success/crash workers after S3 ops to create partial lineage."""

from __future__ import annotations

import argparse
import contextlib
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
def _task(run_id: str, index: int, should_crash: bool, bucket: str) -> dict[str, Any]:
    s3 = _s3_client()
    key = f"worker-crash/{run_id}/task-{index:03d}.txt"
    payload = f"{run_id}|{index}|{time.time_ns()}"
    put_resp = s3.put_object(Bucket=bucket, Key=key, Body=payload.encode("utf-8"))
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    if body != payload:
        raise RuntimeError(f"payload mismatch for {key}")
    if should_crash:
        raise RuntimeError(f"intentional crash after S3 ops for task={index}")
    return {
        "index": index,
        "node_id": _node_id(),
        "path": f"s3://{bucket}/{key}",
        "etag": str((put_resp or {}).get("ETag", "")),
        "status": "ok",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=8)
    parser.add_argument("--crash-count", type=int, default=3)
    parser.add_argument("--bucket", default="test-bucket")
    args = parser.parse_args(argv)

    task_count = max(1, int(args.tasks))
    crash_count = max(0, min(int(args.crash_count), task_count))
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"

    ray.init(address="auto")
    report: dict[str, Any] = {
        "script": "worker_crash_partial_fragments",
        "run_id": run_id,
        "tasks": task_count,
        "crash_count": crash_count,
        "bucket": args.bucket,
        "completed": [],
        "crashed": [],
    }
    try:
        refs: list[tuple[int, ray.ObjectRef]] = []
        for index in range(task_count):
            should_crash = index < crash_count
            refs.append((index, _task.remote(run_id, index, should_crash, str(args.bucket))))

        for index, ref in refs:
            try:
                report["completed"].append(ray.get(ref, timeout=120))
            except Exception as exc:
                report["crashed"].append({"index": index, "error": str(exc)})
        report["completed_count"] = len(report["completed"])
        report["crashed_count"] = len(report["crashed"])
        print(json.dumps(report, sort_keys=True))
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()

    return 1 if report["crashed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
