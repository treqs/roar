#!/usr/bin/env python3
from __future__ import annotations

import argparse
import tempfile
import time
import uuid

import ray

from roar.services.execution.inject.sitecustomize import _prepare_worker_runtime_env
from tests.benchmarks.ray_bench_utils import (
    RAY_ADDRESS,
    benchmark_metadata,
    cleanup_runtime_env,
    format_table,
    linear_regression,
    mean,
    resolve_iterations,
    stdev,
    wait_for_cluster_readiness,
    write_results,
)

DEFAULT_ITERATIONS = 8
QUICK_ITERATIONS = 4
WARMUP_RUNS = 1
RESULT_FILE = "ray_preload_latest.json"

FULL_FILE_COUNTS = [0, 100, 500, 1000, 5000]
QUICK_FILE_COUNTS = [0, 100, 500]
PAYLOAD_BYTES = 256


@ray.remote
def _file_io_task(n_files: int, payload_bytes: int = PAYLOAD_BYTES) -> float:
    payload = b"x" * payload_bytes
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ray-preload-bench-") as temp_dir:
        for idx in range(n_files):
            path = f"{temp_dir}/f{idx}.bin"
            with open(path, "wb") as handle:
                handle.write(payload)
        for idx in range(n_files):
            path = f"{temp_dir}/f{idx}.bin"
            with open(path, "rb") as handle:
                handle.read()
    return time.perf_counter() - start


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Ray worker open() overhead with LD_PRELOAD")
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help=f"Measurement iterations (default: {DEFAULT_ITERATIONS}, quick: {QUICK_ITERATIONS})",
    )
    parser.add_argument("--quick", action="store_true", help="Use reduced file-count sweep")
    return parser.parse_args()


def _run_file_io_once(*, n_files: int) -> float:
    return float(ray.get(_file_io_task.remote(n_files)))


def _measure_mode_for_count(
    *,
    n_files: int,
    iterations: int,
    runtime_env: dict | None,
) -> list[float]:
    ray.init(
        address=RAY_ADDRESS,
        runtime_env=runtime_env,
        ignore_reinit_error=False,
        logging_level="ERROR",
    )
    try:
        for _ in range(WARMUP_RUNS):
            _run_file_io_once(n_files=n_files)
        return [_run_file_io_once(n_files=n_files) for _ in range(iterations)]
    finally:
        ray.shutdown()


def _measure_mode_interleaved(
    *,
    file_counts: list[int],
    iterations: int,
    roar_runtime_env: dict,
) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
    baseline_series: dict[int, list[float]] = {}
    roar_series: dict[int, list[float]] = {}

    for count in file_counts:
        baseline_series[count] = _measure_mode_for_count(
            n_files=count,
            iterations=iterations,
            runtime_env=None,
        )
        roar_series[count] = _measure_mode_for_count(
            n_files=count,
            iterations=iterations,
            runtime_env=roar_runtime_env,
        )

    return baseline_series, roar_series


def main() -> int:
    args = parse_args()
    iterations = resolve_iterations(
        args.iterations,
        default_iterations=DEFAULT_ITERATIONS,
        quick_iterations=QUICK_ITERATIONS,
        quick=args.quick,
    )
    file_counts = QUICK_FILE_COUNTS if args.quick else FULL_FILE_COUNTS

    try:
        wait_for_cluster_readiness()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    roar_runtime_env = _prepare_worker_runtime_env({}, f"preload-bench-{uuid.uuid4().hex[:8]}")
    try:
        baseline_series, roar_series = _measure_mode_interleaved(
            file_counts=file_counts,
            iterations=iterations,
            roar_runtime_env=roar_runtime_env,
        )
    finally:
        cleanup_runtime_env(roar_runtime_env)

    xs = [float(count) for count in file_counts]
    ys = []
    table_rows: list[list[str]] = []

    detailed_rows: list[dict] = []
    for count in file_counts:
        baseline_mean_ms = mean(baseline_series[count]) * 1000.0
        roar_mean_ms = mean(roar_series[count]) * 1000.0
        overhead_ms = roar_mean_ms - baseline_mean_ms
        ys.append(overhead_ms)

        detailed_rows.append(
            {
                "file_count": count,
                "baseline_samples_seconds": baseline_series[count],
                "roar_samples_seconds": roar_series[count],
                "baseline_mean_ms": baseline_mean_ms,
                "baseline_std_ms": stdev(baseline_series[count]) * 1000.0,
                "roar_mean_ms": roar_mean_ms,
                "roar_std_ms": stdev(roar_series[count]) * 1000.0,
                "overhead_ms": overhead_ms,
            }
        )

        table_rows.append(
            [
                str(count),
                f"{baseline_mean_ms:.3f}",
                f"{roar_mean_ms:.3f}",
                f"{overhead_ms:+.3f}",
            ]
        )

    intercept_ms, slope_ms_per_file = linear_regression(xs, ys)
    slope_us_per_open = (slope_ms_per_file * 1000.0) / 2.0

    print("\n--- Ray Worker Preload Overhead ---")
    print(format_table(["Files", "Baseline(ms)", "With roar(ms)", "Overhead(ms)"], table_rows))
    print("\nOLS decomposition (overhead_ms = intercept + slope * files):")
    print(f"  Startup overhead (intercept): {intercept_ms:.4f} ms")
    print(f"  Per-file overhead (slope):    {slope_ms_per_file:.6f} ms")
    print(f"  Per-open overhead:            {slope_us_per_open:.3f} us")

    payload = {
        **benchmark_metadata(ray_version=ray.__version__),
        "results": {
            "iterations": iterations,
            "warmup_runs": WARMUP_RUNS,
            "file_counts": file_counts,
            "ols": {
                "intercept_ms": intercept_ms,
                "slope_ms_per_file": slope_ms_per_file,
                "slope_us_per_open": slope_us_per_open,
            },
            "rows": detailed_rows,
            "projections_ms": {
                str(count): intercept_ms + slope_ms_per_file * count for count in file_counts
            },
        },
    }

    result_path = write_results(RESULT_FILE, payload)
    print(f"\nSaved results to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
