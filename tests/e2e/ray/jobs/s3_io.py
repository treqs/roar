"""Ray job for S3 upload/download using boto3."""

from __future__ import annotations

import os

import boto3
import ray


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("AWS_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name="us-east-1",
    )


@ray.remote
def upload_to_s3(bucket: str, key: str, data: str) -> str:
    s3 = _s3_client()
    s3.put_object(Bucket=bucket, Key=key, Body=data.encode("utf-8"))
    return f"s3://{bucket}/{key}"


@ray.remote
def download_from_s3(bucket: str, key: str) -> str:
    s3 = _s3_client()
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8")


def main() -> None:
    ray.init(address="auto")

    bucket = "test-bucket"
    key = "jobs/s3_io.txt"
    payload = "hello from ray"

    ray.get(upload_to_s3.remote(bucket, key, payload))
    downloaded = ray.get(download_from_s3.remote(bucket, key))

    if downloaded != payload:
        raise ValueError(f"Unexpected S3 payload: {downloaded!r}")

    print("OK")


if __name__ == "__main__":
    main()
