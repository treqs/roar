"""S3 workload that performs Ray work in child subprocesses without ray.shutdown().

This mirrors the cloud demo shape:
  - a parent driver process spawns child Python processes
  - each child calls ray.init(), performs S3 work, and exits normally
  - no child explicitly calls ray.shutdown()
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import boto3
import ray

PHASES = ("extract", "train", "evaluate")
BUCKET = "test-bucket"


def _s3_client(endpoint: str | None):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name="us-east-1",
    )


@ray.remote
def write_then_read(bucket: str, key: str, body: str, endpoint: str | None) -> dict[str, str]:
    s3 = _s3_client(endpoint)
    payload = body.encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=payload)
    value = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    return {"key": key, "body": value}


def _run_phase(phase: str, run_id: str) -> None:
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or None
    ray.init(address="auto", ignore_reinit_error=True, logging_level="ERROR")

    futures = [
        write_then_read.remote(
            BUCKET,
            f"subprocess/{run_id}/{phase}_{index}.txt",
            f"{phase}-{index}",
            endpoint,
        )
        for index in range(3)
    ]
    results = ray.get(futures)
    print(json.dumps({"phase": phase, "results": results}))


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args[:1] == ["--phase"]:
        if len(args) != 3:
            raise SystemExit("usage: s3_subprocess_pipeline.py --phase <name> <run_id>")
        _run_phase(args[1], args[2])
        return 0

    run_id = uuid.uuid4().hex[:8]
    script_path = os.path.abspath(__file__)
    for phase in PHASES:
        subprocess.run(
            [sys.executable, script_path, "--phase", phase, run_id],
            check=True,
        )

    print(json.dumps({"status": "ok", "run_id": run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
