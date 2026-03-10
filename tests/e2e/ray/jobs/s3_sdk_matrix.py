"""Exercise S3 via multiple SDK call paths (and optionally awscli)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

import boto3

import ray


def _session() -> boto3.session.Session:
    return boto3.session.Session()


def _s3_client():
    return _session().client("s3")


def _s3_resource():
    return _session().resource("s3")


def _node_id() -> str:
    try:
        value = ray.get_runtime_context().get_node_id()
        if isinstance(value, bytes):
            return value.hex()
        return str(value)
    except Exception:
        return ""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_boto3_client(bucket: str, key: str, payload: str) -> dict[str, Any]:
    client = _s3_client()
    put_resp = client.put_object(Bucket=bucket, Key=key, Body=payload.encode("utf-8"))
    get_resp = client.get_object(Bucket=bucket, Key=key)
    body = get_resp["Body"].read().decode("utf-8")
    return {
        "method": "boto3.client",
        "write_path": f"s3://{bucket}/{key}",
        "read_path": f"s3://{bucket}/{key}",
        "payload_match": body == payload,
        "etag": str((put_resp or {}).get("ETag", "")),
        "payload_sha256": _sha256_text(body),
    }


def _run_boto3_session_client(bucket: str, key: str, payload: str) -> dict[str, Any]:
    session_client = _session().client("s3")
    put_resp = session_client.put_object(Bucket=bucket, Key=key, Body=payload.encode("utf-8"))
    get_resp = session_client.get_object(Bucket=bucket, Key=key)
    body = get_resp["Body"].read().decode("utf-8")
    return {
        "method": "boto3.Session().client",
        "write_path": f"s3://{bucket}/{key}",
        "read_path": f"s3://{bucket}/{key}",
        "payload_match": body == payload,
        "etag": str((put_resp or {}).get("ETag", "")),
        "payload_sha256": _sha256_text(body),
    }


def _run_boto3_resource(bucket: str, key: str, payload: str) -> dict[str, Any]:
    resource = _s3_resource()
    obj = resource.Object(bucket, key)
    put_resp = obj.put(Body=payload.encode("utf-8"))
    get_resp = obj.get()
    body = get_resp["Body"].read().decode("utf-8")
    return {
        "method": "boto3.resource",
        "write_path": f"s3://{bucket}/{key}",
        "read_path": f"s3://{bucket}/{key}",
        "payload_match": body == payload,
        "etag": str((put_resp or {}).get("ETag", "")),
        "payload_sha256": _sha256_text(body),
    }


def _run_awscli(bucket: str, key: str, payload: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["AWS_EC2_METADATA_DISABLED"] = "true"

    with tempfile.TemporaryDirectory(prefix="awscli-matrix-") as tmpdir:
        src_path = os.path.join(tmpdir, "src.txt")
        dst_path = os.path.join(tmpdir, "dst.txt")
        with open(src_path, "w", encoding="utf-8") as handle:
            handle.write(payload)

        put_cmd = [
            "aws",
            "s3",
            "cp",
            src_path,
            f"s3://{bucket}/{key}",
        ]
        get_cmd = [
            "aws",
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            key,
            dst_path,
        ]
        head_cmd = [
            "aws",
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--output",
            "json",
        ]

        put_result = subprocess.run(
            put_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
        )
        get_result = subprocess.run(
            get_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
        )
        head_result = subprocess.run(
            head_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
        )

        if put_result.returncode != 0 or get_result.returncode != 0 or head_result.returncode != 0:
            raise RuntimeError(
                "awscli commands failed: "
                f"put={put_result.returncode}, get={get_result.returncode}, head={head_result.returncode}"
            )

        with open(dst_path, encoding="utf-8") as handle:
            body = handle.read()

        etag = ""
        try:
            head_payload = json.loads(head_result.stdout)
            etag = str(head_payload.get("ETag", ""))
        except Exception:
            etag = ""

    return {
        "method": "awscli",
        "write_path": f"s3://{bucket}/{key}",
        "read_path": f"s3://{bucket}/{key}",
        "payload_match": body == payload,
        "etag": etag,
        "payload_sha256": _sha256_text(body),
    }


@ray.remote
def _run_method(method: str, run_id: str, bucket: str) -> dict[str, Any]:
    key = f"sdk-matrix/{run_id}/{method.replace('/', '_').replace(' ', '_')}.txt"
    payload = f"{method}|{run_id}|{time.time_ns()}"

    if method == "boto3.client":
        result = _run_boto3_client(bucket, key, payload)
    elif method == "boto3.Session().client":
        result = _run_boto3_session_client(bucket, key, payload)
    elif method == "boto3.resource":
        result = _run_boto3_resource(bucket, key, payload)
    elif method == "awscli":
        result = _run_awscli(bucket, key, payload)
    else:
        raise ValueError(f"Unsupported method: {method}")

    result["node_id"] = _node_id()
    result["key"] = key
    result["expected_payload_sha256"] = _sha256_text(payload)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-awscli", action="store_true")
    parser.add_argument("--bucket", default="test-bucket")
    args = parser.parse_args(argv)

    methods = [
        "boto3.client",
        "boto3.Session().client",
        "boto3.resource",
    ]
    if args.include_awscli:
        methods.append("awscli")

    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    ray.init(address="auto")
    report: dict[str, Any] = {
        "script": "s3_sdk_matrix",
        "run_id": run_id,
        "bucket": args.bucket,
        "methods_requested": methods,
        "results": [],
        "errors": [],
    }
    try:
        refs = {method: _run_method.remote(method, run_id, str(args.bucket)) for method in methods}
        for method, ref in refs.items():
            try:
                payload = ray.get(ref, timeout=180)
                report["results"].append(payload)
            except Exception as exc:
                report["errors"].append({"method": method, "error": str(exc)})

        report["paths_by_method"] = {
            item["method"]: item.get("write_path", "")
            for item in report["results"]
            if isinstance(item, dict) and item.get("method")
        }
        print(json.dumps(report, sort_keys=True))
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()

    if report["errors"]:
        return 1
    if any(not bool(item.get("payload_match")) for item in report["results"] if isinstance(item, dict)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
