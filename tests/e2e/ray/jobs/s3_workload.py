"""Simple S3 workload for proxy-log e2e testing. No roar-specific code."""

from __future__ import annotations

import json
import os

import boto3

import ray


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name="us-east-1",
    )


@ray.remote
def s3_write(bucket: str, key: str, data: str) -> str:
    s3 = _s3_client()
    s3.put_object(Bucket=bucket, Key=key, Body=data.encode("utf-8"))
    return f"s3://{bucket}/{key}"


@ray.remote
def s3_read(bucket: str, key: str) -> str:
    s3 = _s3_client()
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def main() -> None:
    ray.init(address="auto")
    try:
        bucket = "test-bucket"
        key = "proxy-test/data.txt"

        write_uri = ray.get(s3_write.remote(bucket, key, "hello from proxy test"))
        result = ray.get(s3_read.remote(bucket, key))

        print(
            json.dumps(
                {
                    "status": "ok",
                    "write_uri": write_uri,
                    "data": result,
                }
            )
        )
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
