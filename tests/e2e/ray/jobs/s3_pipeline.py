"""3-stage S3 pipeline Ray job.

Stages:
  1. ingest_shard  - reads raw CSV from S3, transforms, writes processed JSON to S3
  2. train_shard   - reads processed JSON, writes model JSON to S3
  3. eval_model    - reads model JSON, writes metrics JSON to S3
                      shard 0 also waits for all metrics and writes final_report.json
  Driver           - orchestrates tasks and prints run metadata

S3 layout:
  test-bucket/raw/{run_id}/shard_{n}.csv
  test-bucket/processed/{run_id}/shard_{n}.json
  output-bucket/models/{run_id}/model_{n}.json
  output-bucket/metrics/{run_id}/metrics_{n}.json
  output-bucket/results/{run_id}/final_report.json
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import boto3
import ray

SHARD_COUNT = 3
TEST_BUCKET = "test-bucket"
OUT_BUCKET = "output-bucket"


def _running_in_ray_worker() -> bool:
    return os.getenv("ROAR_WORKER") == "1"


def _resolve_endpoint_url() -> str:
    endpoint = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint:
        return "http://minio:9000" if _running_in_ray_worker() else "http://localhost:9000"

    parsed = urlparse(endpoint)
    if (
        _running_in_ray_worker()
        and parsed.hostname in {"localhost", "127.0.0.1"}
        and parsed.scheme in {"http", "https"}
    ):
        port = parsed.port or 9000
        patched = parsed._replace(netloc=f"minio:{port}")
        return urlunparse(patched)
    return endpoint


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=_resolve_endpoint_url(),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name="us-east-1",
    )


def _ensure_roar_worker_startup() -> None:
    """
    Best-effort worker startup for Ray Client mode.

    Some Ray client execution paths do not trigger worker setup hooks reliably.
    Calling this inside remote tasks keeps S3/open capture active for live tests.
    """
    try:
        import roar.ray.roar_worker as roar_worker  # noqa: PLC0415

        roar_worker._startup()
    except Exception:  # noqa: BLE001
        return


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


@ray.remote
def ingest_shard(shard_id: int, run_id: str) -> dict[str, Any]:
    """Read raw CSV from S3, transform, write processed JSON back to S3."""
    _ensure_roar_worker_startup()
    s3 = _s3()
    raw_key = f"raw/{run_id}/shard_{shard_id}.csv"
    body = s3.get_object(Bucket=TEST_BUCKET, Key=raw_key)["Body"].read().decode("utf-8")

    rows = []
    for row in body.strip().splitlines():
        if not row:
            continue
        left, right = row.split(",", maxsplit=1)
        rows.append({"id": left, "val": right})

    processed = {"shard_id": shard_id, "run_id": run_id, "rows": rows}
    out_key = f"processed/{run_id}/shard_{shard_id}.json"
    s3.put_object(Bucket=TEST_BUCKET, Key=out_key, Body=json.dumps(processed).encode("utf-8"))
    return {"shard_id": shard_id, "processed_key": f"s3://{TEST_BUCKET}/{out_key}"}


@ray.remote
def train_shard(ingest_result: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Read processed JSON and produce a minimal model artifact."""
    _ensure_roar_worker_startup()
    s3 = _s3()
    bucket, key = _parse_s3_uri(str(ingest_result["processed_key"]))
    data = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())

    shard_id = int(data["shard_id"])
    model = {
        "shard_id": shard_id,
        "run_id": run_id,
        "weights": [len(str(row.get("val", ""))) for row in data.get("rows", [])],
    }
    model_key = f"models/{run_id}/model_{shard_id}.json"
    s3.put_object(Bucket=OUT_BUCKET, Key=model_key, Body=json.dumps(model).encode("utf-8"))
    return {"shard_id": shard_id, "model_key": f"s3://{OUT_BUCKET}/{model_key}"}


@ray.remote
def eval_model(train_result: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Read model and produce metrics."""
    _ensure_roar_worker_startup()
    s3 = _s3()
    bucket, key = _parse_s3_uri(str(train_result["model_key"]))
    model = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())

    shard_id = int(model["shard_id"])
    weights = [float(value) for value in model.get("weights", [])]
    score = sum(weights) / max(len(weights), 1)

    metrics = {"shard_id": shard_id, "run_id": run_id, "score": score}
    metrics_key = f"metrics/{run_id}/metrics_{shard_id}.json"
    s3.put_object(Bucket=OUT_BUCKET, Key=metrics_key, Body=json.dumps(metrics).encode("utf-8"))

    report_uri: str | None = None
    if shard_id == 0:
        deadline = time.time() + 45
        all_metrics: list[dict[str, Any]] = []
        while time.time() < deadline:
            all_metrics = []
            complete = True
            for idx in range(SHARD_COUNT):
                key = f"metrics/{run_id}/metrics_{idx}.json"
                try:
                    payload = json.loads(
                        s3.get_object(Bucket=OUT_BUCKET, Key=key)["Body"].read()
                    )
                    all_metrics.append(payload)
                except Exception:  # noqa: BLE001
                    complete = False
                    break
            if complete:
                break
            time.sleep(0.2)

        if len(all_metrics) == SHARD_COUNT:
            report = {
                "run_id": run_id,
                "shards": SHARD_COUNT,
                "avg_score": sum(float(item["score"]) for item in all_metrics) / SHARD_COUNT,
            }
            report_key = f"results/{run_id}/final_report.json"
            s3.put_object(
                Bucket=OUT_BUCKET,
                Key=report_key,
                Body=json.dumps(report).encode("utf-8"),
            )
            report_uri = f"s3://{OUT_BUCKET}/{report_key}"

    return {
        "shard_id": shard_id,
        "metrics_key": f"s3://{OUT_BUCKET}/{metrics_key}",
        "report_key": report_uri,
    }


def main(ray_address: str = "auto") -> str:
    run_id = uuid.uuid4().hex[:8]
    s3 = _s3()

    for shard_id in range(SHARD_COUNT):
        body = "\n".join(f"{idx},{chr(65 + shard_id)}{idx}" for idx in range(5))
        s3.put_object(
            Bucket=TEST_BUCKET,
            Key=f"raw/{run_id}/shard_{shard_id}.csv",
            Body=body.encode("utf-8"),
        )

    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")

    ingest_results = ray.get([ingest_shard.remote(idx, run_id) for idx in range(SHARD_COUNT)])
    train_results = ray.get([train_shard.remote(result, run_id) for result in ingest_results])
    eval_results = ray.get([eval_model.remote(result, run_id) for result in train_results])

    report_key = None
    for item in eval_results:
        maybe = item.get("report_key")
        if isinstance(maybe, str) and maybe.startswith("s3://"):
            report_key = maybe
            break
    if report_key is None:
        raise RuntimeError(f"No final report produced for run_id={run_id}")

    ray.shutdown()
    print(json.dumps({"run_id": run_id, "report_key": report_key}))
    return run_id


if __name__ == "__main__":
    address = sys.argv[1] if len(sys.argv) > 1 else "auto"
    main(address)
