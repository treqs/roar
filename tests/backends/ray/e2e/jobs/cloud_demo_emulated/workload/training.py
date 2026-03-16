"""Training workload matching the real cloud-demo shape."""

from __future__ import annotations

import io
import json

import pyarrow.parquet as pq
from cloud_demo_emulated.workload.aws_client import resolve_s3_endpoint, s3_client
from cloud_demo_emulated.workload.config import NUM_EPOCHS, S3_DATA_BUCKET, S3_MODELS_BUCKET

import ray


@ray.remote
def train_on_shard(
    shard_key: str,
    model_state: bytes | None,
    bucket: str,
    endpoint: str | None,
) -> bytes:
    s3 = s3_client(endpoint_url=endpoint)
    obj = s3.get_object(Bucket=bucket, Key=shard_key)
    table = pq.read_table(io.BytesIO(obj["Body"].read()))

    frame_count = table.num_rows
    position_sum = float(sum(table["position_x"].to_pylist()))
    prior_weight = 0.0
    if model_state:
        prior_weight = float(json.loads(model_state.decode("utf-8"))["weight"])
    next_state = {
        "weight": prior_weight + frame_count + position_sum,
        "frames": frame_count,
        "source_shard": shard_key,
    }
    return json.dumps(next_state).encode("utf-8")


def run_training(shard_keys: list[str], run_id: str, ray_address: str = "auto") -> str:
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        endpoint = resolve_s3_endpoint()
        model_state: bytes | None = None
        for _epoch in range(NUM_EPOCHS):
            futures = [
                train_on_shard.remote(shard_key, model_state, S3_DATA_BUCKET, endpoint)
                for shard_key in shard_keys
            ]
            results = ray.get(futures)
            model_state = results[-1]
    finally:
        ray.shutdown()

    assert model_state is not None
    model_key = f"models/{run_id}/sensor_predictor_final.json"
    s3 = s3_client(endpoint_url=resolve_s3_endpoint())
    s3.put_object(Bucket=S3_MODELS_BUCKET, Key=model_key, Body=model_state)
    return model_key
