"""Force multipart upload and verify object round-trip."""

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
def _multipart_upload(run_id: str, bucket: str, parts: int, part_size_mb: int) -> dict[str, Any]:
    client = _s3_client()
    key = f"multipart/{run_id}/large-object.bin"
    part_size = max(5, int(part_size_mb)) * 1024 * 1024
    part_count = max(2, int(parts))

    created = client.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = str(created["UploadId"])

    completed_parts: list[dict[str, Any]] = []
    total_size = 0
    try:
        for index in range(part_count):
            # Keep payload deterministic and large enough for multipart semantics.
            payload = (bytes([65 + (index % 20)]) * part_size)
            total_size += len(payload)
            result = client.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=index + 1,
                Body=payload,
            )
            completed_parts.append({"PartNumber": index + 1, "ETag": result["ETag"]})

        completed = client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": completed_parts},
        )
    except Exception:
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise

    head = client.head_object(Bucket=bucket, Key=key)
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read(128)
    return {
        "node_id": _node_id(),
        "path": f"s3://{bucket}/{key}",
        "key": key,
        "parts": part_count,
        "part_size_bytes": part_size,
        "uploaded_size_bytes": total_size,
        "head_size_bytes": int(head.get("ContentLength", 0)),
        "multipart_etag": str(head.get("ETag", "")),
        "complete_etag": str((completed or {}).get("ETag", "")),
        "first_bytes_len": len(body),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="output-bucket")
    parser.add_argument("--parts", type=int, default=3)
    parser.add_argument("--part-size-mb", type=int, default=6)
    args = parser.parse_args(argv)

    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    ray.init(address="auto")
    try:
        result = ray.get(
            _multipart_upload.remote(
                run_id,
                str(args.bucket),
                int(args.parts),
                int(args.part_size_mb),
            ),
            timeout=300,
        )
        report = {
            "script": "s3_multipart_io",
            "run_id": run_id,
            "bucket": args.bucket,
            "result": result,
        }
        print(json.dumps(report, sort_keys=True))
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
