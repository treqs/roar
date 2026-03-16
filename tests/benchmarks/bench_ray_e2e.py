#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import tempfile
import time
import uuid

import ray

from roar.backends.ray.plugin import ray_prepare_worker_runtime_env as _prepare_worker_runtime_env
from tests.benchmarks.ray_bench_utils import (
    MINIO_ACCESS_KEY,
    MINIO_INTERNAL_ENDPOINT,
    MINIO_SECRET_KEY,
    RAY_ADDRESS,
    benchmark_metadata,
    cleanup_runtime_env,
    ensure_minio_bucket,
    format_table,
    mean,
    percent_delta,
    resolve_iterations,
    stdev,
    wait_for_cluster_readiness,
    write_results,
)

DEFAULT_ITERATIONS = 10
QUICK_ITERATIONS = 5
WARMUP_RUNS = 1
RESULT_FILE = "ray_e2e_latest.json"
BUCKET = "bench-e2e"

FULL_TASK_COUNTS = [1, 4, 16]
QUICK_TASK_COUNTS = [1, 4]

FULL_IO_LEVELS = {
    "low": {"files": 10, "s3_ops": 0},
    "medium": {"files": 100, "s3_ops": 5},
    "high": {"files": 1000, "s3_ops": 20},
}
QUICK_IO_LEVELS = {
    "low": FULL_IO_LEVELS["low"],
    "medium": FULL_IO_LEVELS["medium"],
}


@ray.remote
def _reference_task(
    task_index: int, n_files: int, n_s3_ops: int, bucket: str, key_prefix: str
) -> int:
    import boto3

    endpoint = os.getenv("AWS_ENDPOINT_URL", "http://minio:9000")
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )

    payload = b"x" * 4096
    with tempfile.TemporaryDirectory(prefix=f"ray-e2e-task-{task_index}-") as tmp_dir:
        for file_index in range(n_files):
            path = os.path.join(tmp_dir, f"f{file_index}.bin")
            with open(path, "wb") as handle:
                handle.write(payload)
            with open(path, "rb") as handle:
                handle.read()

    s3_payload = b"z" * 1024
    for op_index in range(n_s3_ops):
        key = f"{key_prefix}/task-{task_index}/obj-{op_index}.bin"
        s3.put_object(Bucket=bucket, Key=key, Body=s3_payload)
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        body.read()
        body.close()

    return n_files + n_s3_ops


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Ray end-to-end job overhead with roar")
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help=f"Measurement iterations (default: {DEFAULT_ITERATIONS}, quick: {QUICK_ITERATIONS})",
    )
    parser.add_argument("--quick", action="store_true", help="Use reduced scenario set")
    return parser.parse_args()


def _run_job_once(
    *, task_count: int, n_files: int, n_s3_ops: int, runtime_env: dict | None
) -> float:
    key_prefix = f"bench-e2e/{uuid.uuid4().hex[:12]}"
    start = time.perf_counter()
    ray.init(
        address=RAY_ADDRESS,
        runtime_env=runtime_env,
        ignore_reinit_error=False,
        logging_level="ERROR",
    )
    try:
        refs = [
            _reference_task.remote(task_index, n_files, n_s3_ops, BUCKET, key_prefix)
            for task_index in range(task_count)
        ]
        ray.get(refs)
    finally:
        ray.shutdown()
    return time.perf_counter() - start


def _measure_scenario_interleaved(
    *,
    task_count: int,
    n_files: int,
    n_s3_ops: int,
    iterations: int,
    roar_runtime_env: dict,
) -> tuple[list[float], list[float]]:
    for _ in range(WARMUP_RUNS):
        _run_job_once(
            task_count=task_count,
            n_files=n_files,
            n_s3_ops=n_s3_ops,
            runtime_env=None,
        )
        _run_job_once(
            task_count=task_count,
            n_files=n_files,
            n_s3_ops=n_s3_ops,
            runtime_env=roar_runtime_env,
        )

    baseline_samples: list[float] = []
    roar_samples: list[float] = []
    for _ in range(iterations):
        baseline_samples.append(
            _run_job_once(
                task_count=task_count,
                n_files=n_files,
                n_s3_ops=n_s3_ops,
                runtime_env=None,
            )
        )
        roar_samples.append(
            _run_job_once(
                task_count=task_count,
                n_files=n_files,
                n_s3_ops=n_s3_ops,
                runtime_env=roar_runtime_env,
            )
        )

    return baseline_samples, roar_samples


def _make_roar_runtime_env() -> dict:
    runtime_env = _prepare_worker_runtime_env({}, f"e2e-bench-{uuid.uuid4().hex[:8]}", {})
    env_vars = dict(runtime_env.get("env_vars", {}))
    env_vars.update(
        {
            "ROAR_JOB_ID": f"bench-{uuid.uuid4().hex[:8]}",
            "AWS_ENDPOINT_URL": MINIO_INTERNAL_ENDPOINT,
            "AWS_ACCESS_KEY_ID": MINIO_ACCESS_KEY,
            "AWS_SECRET_ACCESS_KEY": MINIO_SECRET_KEY,
            "AWS_DEFAULT_REGION": "us-east-1",
        }
    )
    runtime_env["env_vars"] = env_vars
    return runtime_env


def main() -> int:
    args = parse_args()
    iterations = resolve_iterations(
        args.iterations,
        default_iterations=DEFAULT_ITERATIONS,
        quick_iterations=QUICK_ITERATIONS,
        quick=args.quick,
    )

    task_counts = QUICK_TASK_COUNTS if args.quick else FULL_TASK_COUNTS
    io_levels = QUICK_IO_LEVELS if args.quick else FULL_IO_LEVELS

    try:
        wait_for_cluster_readiness()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    ensure_minio_bucket(BUCKET)

    roar_runtime_env = _make_roar_runtime_env()
    rows: list[dict] = []

    try:
        for io_name, cfg in io_levels.items():
            for task_count in task_counts:
                baseline_samples, roar_samples = _measure_scenario_interleaved(
                    task_count=task_count,
                    n_files=cfg["files"],
                    n_s3_ops=cfg["s3_ops"],
                    iterations=iterations,
                    roar_runtime_env=roar_runtime_env,
                )

                baseline_mean = mean(baseline_samples)
                roar_mean = mean(roar_samples)

                rows.append(
                    {
                        "io_level": io_name,
                        "task_count": task_count,
                        "files_per_task": cfg["files"],
                        "s3_ops_per_task": cfg["s3_ops"],
                        "baseline_samples_seconds": baseline_samples,
                        "roar_samples_seconds": roar_samples,
                        "baseline_mean_seconds": baseline_mean,
                        "baseline_std_seconds": stdev(baseline_samples),
                        "roar_mean_seconds": roar_mean,
                        "roar_std_seconds": stdev(roar_samples),
                        "overhead_ms": (roar_mean - baseline_mean) * 1000.0,
                        "overhead_percent": percent_delta(roar_mean, baseline_mean),
                    }
                )
    finally:
        cleanup_runtime_env(roar_runtime_env)

    table_rows = [
        [
            f"tasks={row['task_count']} io={row['io_level']}",
            f"{row['baseline_mean_seconds']:.3f}",
            f"{row['roar_mean_seconds']:.3f}",
            f"{row['overhead_ms']:+.1f}",
            f"{row['overhead_percent']:+.1f}%",
        ]
        for row in rows
    ]

    print("\n--- Ray End-to-End Job Overhead ---")
    print(
        format_table(
            ["Scenario", "Baseline(s)", "With roar(s)", "Overhead(ms)", "Overhead%"],
            table_rows,
        )
    )

    payload = {
        **benchmark_metadata(ray_version=ray.__version__),
        "results": {
            "iterations": iterations,
            "warmup_runs": WARMUP_RUNS,
            "task_counts": task_counts,
            "io_levels": io_levels,
            "rows": rows,
        },
    }

    result_path = write_results(RESULT_FILE, payload)
    print(f"\nSaved results to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
