"""Extraction workload matching the real cloud-demo shape."""

from __future__ import annotations

import io

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from cloud_demo_emulated.workload.aws_client import resolve_s3_endpoint, s3_client
from cloud_demo_emulated.workload.config import NUM_FRAMES_PER_FILE, S3_DATA_BUCKET, SHARD_COUNT

import ray


@ray.remote
def generate_sensor_shard(
    shard_id: int, num_frames: int, bucket: str, endpoint: str | None
) -> dict:
    rng = np.random.default_rng(shard_id)
    table = pa.table(
        {
            "frame_id": pa.array(range(num_frames)),
            "position_x": pa.array(rng.normal(0, 1, num_frames).astype(np.float32)),
            "position_y": pa.array(rng.normal(0, 1, num_frames).astype(np.float32)),
            "position_z": pa.array(rng.normal(0, 1, num_frames).astype(np.float32)),
            "depth_mean": pa.array(rng.uniform(1.0, 10.0, num_frames).astype(np.float32)),
        }
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    buffer.seek(0)

    key = f"sensor_data/shard_{shard_id:06d}.parquet"
    s3 = s3_client(endpoint_url=endpoint)
    s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
    return {"shard_id": shard_id, "key": key}


def run_extraction(run_id: str, ray_address: str = "auto") -> list[str]:
    del run_id
    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="ERROR")
    try:
        endpoint = resolve_s3_endpoint()
        futures = [
            generate_sensor_shard.remote(index, NUM_FRAMES_PER_FILE, S3_DATA_BUCKET, endpoint)
            for index in range(SHARD_COUNT)
        ]
        results = ray.get(futures)
    finally:
        ray.shutdown()
    ordered = sorted(results, key=lambda item: int(item["shard_id"]))
    return [str(item["key"]) for item in ordered]
