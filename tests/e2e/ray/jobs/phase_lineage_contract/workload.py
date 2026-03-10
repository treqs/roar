"""Small single-path Ray workload with explicit extract/train/evaluate phases."""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import boto3

import ray

DATA_BUCKET = "test-bucket"
RESULTS_BUCKET = "output-bucket"


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
def extract_dataset(run_id: str) -> str:
    s3 = _s3()
    payload = {
        "run_id": run_id,
        "records": [2, 4, 6, 8],
        "source": "synthetic",
    }
    key = f"phase-lineage/{run_id}/processed/features.json"
    s3.put_object(Bucket=DATA_BUCKET, Key=key, Body=json.dumps(payload).encode("utf-8"))
    return f"s3://{DATA_BUCKET}/{key}"


def run_extraction(run_id: str, ray_address: str = "auto") -> str:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        return str(ray.get(extract_dataset.remote(run_id)))
    finally:
        ray.shutdown()


@ray.remote
def train_model(processed_key: str, run_id: str) -> str:
    s3 = _s3()
    bucket, key = _parse_s3_uri(processed_key)
    payload = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    model = {
        "run_id": run_id,
        "record_count": len(payload.get("records", [])),
        "weight": sum(int(value) for value in payload.get("records", [])),
    }
    model_key = f"phase-lineage/{run_id}/models/model.json"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=model_key, Body=json.dumps(model).encode("utf-8"))
    return f"s3://{RESULTS_BUCKET}/{model_key}"


def run_training(processed_key: str, run_id: str, ray_address: str = "auto") -> str:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        return str(ray.get(train_model.remote(processed_key, run_id)))
    finally:
        ray.shutdown()


@ray.remote
def evaluate_model(model_key: str, run_id: str) -> str:
    s3 = _s3()
    bucket, key = _parse_s3_uri(model_key)
    model = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    report = {
        "run_id": run_id,
        "score": float(model["weight"]) / max(int(model["record_count"]), 1),
        "status": "ok",
    }
    report_key = f"phase-lineage/{run_id}/reports/final_report.json"
    s3.put_object(Bucket=RESULTS_BUCKET, Key=report_key, Body=json.dumps(report).encode("utf-8"))
    return f"s3://{RESULTS_BUCKET}/{report_key}"


def run_evaluation(model_key: str, run_id: str, ray_address: str = "auto") -> str:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        return str(ray.get(evaluate_model.remote(model_key, run_id)))
    finally:
        ray.shutdown()
