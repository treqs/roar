"""Small S3-backed Ray workload with the same phase shape as cloud-demo."""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import boto3
import ray

DATA_BUCKET = "test-bucket"
RESULTS_BUCKET = "output-bucket"
SHARD_COUNT = 3


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("AWS_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name="us-east-1",
    )


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


@ray.remote
def extract_shard(shard_id: int, run_id: str) -> dict[str, object]:
    s3 = _s3()
    payload = {
        "run_id": run_id,
        "shard_id": shard_id,
        "values": [shard_id + 1, shard_id + 2, shard_id + 3],
    }
    key = f"cloud-demo-like/{run_id}/processed/shard_{shard_id}.json"
    s3.put_object(Bucket=DATA_BUCKET, Key=key, Body=json.dumps(payload).encode("utf-8"))
    return {"shard_id": shard_id, "processed_key": f"s3://{DATA_BUCKET}/{key}"}


def run_extraction(run_id: str, ray_address: str = "auto") -> list[str]:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        results = ray.get([extract_shard.remote(index, run_id) for index in range(SHARD_COUNT)])
    finally:
        ray.shutdown()
    return [str(item["processed_key"]) for item in sorted(results, key=lambda item: int(item["shard_id"]))]


@ray.remote
def train_on_shard(processed_key: str, run_id: str) -> dict[str, object]:
    s3 = _s3()
    bucket, key = _parse_s3_uri(processed_key)
    payload = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    shard_id = int(payload["shard_id"])
    model = {
        "run_id": run_id,
        "shard_id": shard_id,
        "weight": sum(int(value) for value in payload.get("values", [])),
    }
    model_key = f"cloud-demo-like/{run_id}/models/model_{shard_id}.json"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=model_key, Body=json.dumps(model).encode("utf-8"))
    return {"shard_id": shard_id, "model_key": f"s3://{RESULTS_BUCKET}/{model_key}"}


def run_training(processed_keys: list[str], run_id: str, ray_address: str = "auto") -> list[str]:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        results = ray.get([train_on_shard.remote(key, run_id) for key in processed_keys])
    finally:
        ray.shutdown()
    return [str(item["model_key"]) for item in sorted(results, key=lambda item: int(item["shard_id"]))]


@ray.remote
def evaluate_shard(model_key: str, run_id: str) -> dict[str, object]:
    s3 = _s3()
    bucket, key = _parse_s3_uri(model_key)
    model = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    shard_id = int(model["shard_id"])
    metrics = {
        "run_id": run_id,
        "shard_id": shard_id,
        "score": float(model["weight"]) / max(shard_id + 1, 1),
    }
    metrics_key = f"cloud-demo-like/{run_id}/metrics/metric_{shard_id}.json"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=metrics_key, Body=json.dumps(metrics).encode("utf-8"))
    return {"shard_id": shard_id, "metrics_key": f"s3://{RESULTS_BUCKET}/{metrics_key}", "score": metrics["score"]}


def run_evaluation(model_keys: list[str], run_id: str, ray_address: str = "auto") -> str:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        results = ray.get([evaluate_shard.remote(key, run_id) for key in model_keys])
    finally:
        ray.shutdown()

    report = {
        "run_id": run_id,
        "scores": [float(item["score"]) for item in sorted(results, key=lambda item: int(item["shard_id"]))],
    }
    report["avg_score"] = sum(report["scores"]) / max(len(report["scores"]), 1)

    s3 = _s3()
    report_key = f"cloud-demo-like/{run_id}/results/final_report.json"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=report_key, Body=json.dumps(report).encode("utf-8"))
    return f"s3://{RESULTS_BUCKET}/{report_key}"

