"""Timing-focused Ray workload for lineage contract tests."""

from __future__ import annotations

import json
import os
import time

import boto3

import ray

TASK_PRE_IO_SLEEP_SECONDS = 1.4
TASK_POST_IO_SLEEP_SECONDS = 0.8
RESULTS_BUCKET = "output-bucket"


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("AWS_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name="us-east-1",
    )


@ray.remote
def timed_write(run_id: str) -> dict[str, object]:
    started_at = time.time()
    time.sleep(TASK_PRE_IO_SLEEP_SECONDS)

    key = f"timing-contract/{run_id}/timed_write.json"
    payload = {
        "run_id": run_id,
        "task_started_at": started_at,
        "payload_written_at": time.time(),
    }
    _s3().put_object(
        Bucket=RESULTS_BUCKET,
        Key=key,
        Body=json.dumps(payload, sort_keys=True).encode("utf-8"),
    )

    time.sleep(TASK_POST_IO_SLEEP_SECONDS)
    ended_at = time.time()
    return {
        "artifact_path": f"s3://{RESULTS_BUCKET}/{key}",
        "report_key": key,
        "task_started_at": started_at,
        "task_ended_at": ended_at,
        "expected_duration_seconds": TASK_PRE_IO_SLEEP_SECONDS + TASK_POST_IO_SLEEP_SECONDS,
    }


def run_phase(run_id: str, ray_address: str = "auto") -> dict[str, object]:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        return dict(ray.get(timed_write.remote(run_id)))
    finally:
        ray.shutdown()
