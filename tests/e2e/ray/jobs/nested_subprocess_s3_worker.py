"""Nested subprocess worker that performs one remote S3 write."""

from __future__ import annotations

import argparse
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
def write_s3(run_id: str) -> str:
    key = f"nested-subprocess/{run_id}/data.txt"
    _s3_client().put_object(Bucket="test-bucket", Key=key, Body=b"nested subprocess")
    return f"s3://test-bucket/{key}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    ray.init(address="auto")
    try:
        output_uri = ray.get(write_s3.remote(str(args.run_id)))
    finally:
        ray.shutdown()
    print(json.dumps({"output_uri": output_uri}, sort_keys=True))


if __name__ == "__main__":
    main()
