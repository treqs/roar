"""Evaluation workload matching the real cloud-demo shape."""

from __future__ import annotations

import io
import json

import pyarrow.parquet as pq
import ray

from cloud_demo_emulated.workload.aws_client import resolve_s3_endpoint, s3_client
from cloud_demo_emulated.workload.config import EVAL_LIMIT, S3_DATA_BUCKET, S3_MODELS_BUCKET, S3_RESULTS_BUCKET


@ray.remote
def evaluate_shard(
    shard_key: str,
    model_key: str,
    data_bucket: str,
    models_bucket: str,
    endpoint: str | None,
) -> dict:
    s3 = s3_client(endpoint_url=endpoint)
    model_bytes = s3.get_object(Bucket=models_bucket, Key=model_key)["Body"].read()
    model = json.loads(model_bytes.decode("utf-8"))
    obj = s3.get_object(Bucket=data_bucket, Key=shard_key)
    table = pq.read_table(io.BytesIO(obj["Body"].read()))
    score = float(model["weight"]) / max(table.num_rows, 1)
    return {"shard": shard_key, "score": score}


def run_evaluation(model_key: str, shard_keys: list[str], run_id: str, ray_address: str = "auto") -> str:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        endpoint = resolve_s3_endpoint()
        eval_shards = shard_keys[:EVAL_LIMIT]
        futures = [
            evaluate_shard.remote(shard_key, model_key, S3_DATA_BUCKET, S3_MODELS_BUCKET, endpoint)
            for shard_key in eval_shards
        ]
        results = ray.get(futures)
    finally:
        ray.shutdown()

    summary = {
        "run_id": run_id,
        "avg_score": float(sum(item["score"] for item in results) / max(len(results), 1)),
        "num_shards_evaluated": len(results),
    }
    metrics_key = f"evaluation/{run_id}/metrics.json"
    s3 = s3_client(endpoint_url=resolve_s3_endpoint())
    s3.put_object(
        Bucket=S3_RESULTS_BUCKET,
        Key=metrics_key,
        Body=json.dumps(summary, sort_keys=True).encode("utf-8"),
    )
    return metrics_key
