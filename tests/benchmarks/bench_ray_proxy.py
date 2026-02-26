#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
import uuid

import ray

from roar.services.execution.proxy import ProxyService
from tests.benchmarks.ray_bench_utils import (
    MINIO_ACCESS_KEY,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    benchmark_metadata,
    ensure_minio_bucket,
    ensure_ray_docker_cluster_running,
    format_table,
    mean,
    minio_client,
    percent_delta,
    resolve_iterations,
    stdev,
    write_results,
)

DEFAULT_ITERATIONS = 10
QUICK_ITERATIONS = 5
WARMUP_RUNS = 1
RESULT_FILE = "ray_proxy_latest.json"
BUCKET = "bench-proxy"
PROXY_UPSTREAM_ENDPOINT = "http://127.0.0.1:9000"

FULL_SIZES = {
    "1KB": 1024,
    "1MB": 1024 * 1024,
    "100MB": 100 * 1024 * 1024,
}
QUICK_SIZES = {
    "1KB": 1024,
    "1MB": 1024 * 1024,
}
OPERATIONS = ["PutObject", "GetObject"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark roar-proxy S3 latency overhead")
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help=f"Measurement iterations (default: {DEFAULT_ITERATIONS}, quick: {QUICK_ITERATIONS})",
    )
    parser.add_argument("--quick", action="store_true", help="Use reduced object size set")
    return parser.parse_args()


def _measure_put(client, *, bucket: str, key: str, payload: bytes) -> float:
    start = time.perf_counter()
    client.put_object(Bucket=bucket, Key=key, Body=payload)
    return time.perf_counter() - start


def _measure_get(client, *, bucket: str, key: str) -> float:
    start = time.perf_counter()
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    body.read()
    body.close()
    return time.perf_counter() - start


def main() -> int:
    args = parse_args()
    iterations = resolve_iterations(
        args.iterations,
        default_iterations=DEFAULT_ITERATIONS,
        quick_iterations=QUICK_ITERATIONS,
        quick=args.quick,
    )

    sizes = QUICK_SIZES if args.quick else FULL_SIZES

    try:
        ensure_ray_docker_cluster_running()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    ensure_minio_bucket(BUCKET, endpoint_url=MINIO_ENDPOINT)

    # roar-proxy signs forwarded requests and needs AWS credentials in env.
    os.environ["AWS_ACCESS_KEY_ID"] = MINIO_ACCESS_KEY
    os.environ["AWS_SECRET_ACCESS_KEY"] = MINIO_SECRET_KEY
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

    proxy_service = ProxyService()
    try:
        proxy_handle = proxy_service.start_for_run(
            session_id="bench-ray-proxy",
            job_id=f"bench-{uuid.uuid4().hex[:8]}",
            upstream_url=PROXY_UPSTREAM_ENDPOINT,
        )
    except RuntimeError as exc:
        print(f"ERROR: failed to start roar-proxy: {exc}")
        return 2

    proxy_endpoint = f"http://127.0.0.1:{proxy_handle.port}"
    direct_client = minio_client(MINIO_ENDPOINT)
    proxy_client = minio_client(proxy_endpoint)

    rows: list[dict] = []

    try:
        for size_label, size_bytes in sizes.items():
            payload = b"x" * size_bytes
            seed_key = f"bench/get-seed/{size_label}.bin"
            direct_client.put_object(Bucket=BUCKET, Key=seed_key, Body=payload)

            for operation in OPERATIONS:
                direct_samples: list[float] = []
                proxy_samples: list[float] = []

                for _ in range(WARMUP_RUNS):
                    warmup_key = f"bench/warmup/{operation}/{size_label}/{uuid.uuid4().hex}"
                    if operation == "PutObject":
                        _measure_put(direct_client, bucket=BUCKET, key=warmup_key, payload=payload)
                        _measure_put(proxy_client, bucket=BUCKET, key=warmup_key + "-proxy", payload=payload)
                    else:
                        _measure_get(direct_client, bucket=BUCKET, key=seed_key)
                        _measure_get(proxy_client, bucket=BUCKET, key=seed_key)

                for iteration_idx in range(iterations):
                    if operation == "PutObject":
                        direct_key = f"bench/put/direct/{size_label}/{iteration_idx}-{uuid.uuid4().hex}"
                        proxy_key = f"bench/put/proxy/{size_label}/{iteration_idx}-{uuid.uuid4().hex}"
                        direct_samples.append(
                            _measure_put(direct_client, bucket=BUCKET, key=direct_key, payload=payload)
                        )
                        proxy_samples.append(
                            _measure_put(proxy_client, bucket=BUCKET, key=proxy_key, payload=payload)
                        )
                    else:
                        direct_samples.append(_measure_get(direct_client, bucket=BUCKET, key=seed_key))
                        proxy_samples.append(_measure_get(proxy_client, bucket=BUCKET, key=seed_key))

                direct_mean_ms = mean(direct_samples) * 1000.0
                proxy_mean_ms = mean(proxy_samples) * 1000.0
                rows.append(
                    {
                        "operation": operation,
                        "size_label": size_label,
                        "size_bytes": size_bytes,
                        "direct_samples_seconds": direct_samples,
                        "proxy_samples_seconds": proxy_samples,
                        "direct_mean_ms": direct_mean_ms,
                        "direct_std_ms": stdev(direct_samples) * 1000.0,
                        "proxy_mean_ms": proxy_mean_ms,
                        "proxy_std_ms": stdev(proxy_samples) * 1000.0,
                        "overhead_ms": proxy_mean_ms - direct_mean_ms,
                        "overhead_percent": percent_delta(proxy_mean_ms, direct_mean_ms),
                    }
                )
    finally:
        proxy_service.stop_for_run(proxy_handle)

    table_rows = [
        [
            row["operation"],
            row["size_label"],
            f"{row['direct_mean_ms']:.3f}",
            f"{row['proxy_mean_ms']:.3f}",
            f"{row['overhead_ms']:+.3f}",
            f"{row['overhead_percent']:+.1f}%",
        ]
        for row in rows
    ]

    print("\n--- Ray Proxy S3 Overhead ---")
    print(
        format_table(
            ["Operation", "Size", "Direct(ms)", "Via proxy(ms)", "Overhead(ms)", "Overhead%"],
            table_rows,
        )
    )

    payload = {
        "proxy_endpoint": proxy_endpoint,
        "proxy_upstream_endpoint": PROXY_UPSTREAM_ENDPOINT,
        **benchmark_metadata(ray_version=ray.__version__),
        "results": {
            "iterations": iterations,
            "warmup_runs": WARMUP_RUNS,
            "rows": rows,
        },
    }

    result_path = write_results(RESULT_FILE, payload)
    print(f"\nSaved results to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
