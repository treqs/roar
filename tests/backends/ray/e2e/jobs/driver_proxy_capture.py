"""Driver-only S3 workload to exercise the driver_entrypoint proxy path."""

from __future__ import annotations

import json
import os
import uuid

import boto3


def main() -> None:
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or None
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

    run_id = uuid.uuid4().hex[:8]
    key = f"driver/{run_id}/driver_proxy_capture.txt"
    payload = f"driver proxy capture {run_id}\n".encode()

    s3.put_object(Bucket="test-bucket", Key=key, Body=payload)
    body = s3.get_object(Bucket="test-bucket", Key=key)["Body"].read().decode("utf-8")

    print(
        json.dumps(
            {
                "run_id": run_id,
                "key": key,
                "body": body,
                "aws_endpoint_url": os.environ.get("AWS_ENDPOINT_URL", ""),
                "roar_proxy_port": os.environ.get("ROAR_PROXY_PORT", ""),
            }
        )
    )


if __name__ == "__main__":
    main()
