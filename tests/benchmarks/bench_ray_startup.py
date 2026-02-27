#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

import ray

from roar.services.execution.inject.sitecustomize import _prepare_worker_runtime_env
from tests.benchmarks.ray_bench_utils import (
    RAY_ADDRESS,
    benchmark_metadata,
    cleanup_runtime_env,
    format_table,
    mean,
    percent_delta,
    resolve_iterations,
    stdev,
    wait_for_cluster_readiness,
    write_results,
)

DEFAULT_ITERATIONS = 5
QUICK_ITERATIONS = 3
WARMUP_RUNS = 1
RESULT_FILE = "ray_startup_latest.json"


@ray.remote
def _noop() -> int:
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Ray startup overhead with roar runtime_env"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help=f"Measurement iterations (default: {DEFAULT_ITERATIONS}, quick: {QUICK_ITERATIONS})",
    )
    parser.add_argument("--quick", action="store_true", help="Use reduced iteration count")
    return parser.parse_args()


def _single_startup_run(runtime_env: dict | None) -> float:
    start = time.perf_counter()
    ray.init(
        address=RAY_ADDRESS,
        runtime_env=runtime_env,
        ignore_reinit_error=False,
        logging_level="ERROR",
    )
    ray.get(_noop.remote())
    elapsed = time.perf_counter() - start
    ray.shutdown()
    return elapsed


def _measure_runs_interleaved(
    baseline_run_once,
    roar_run_once,
    *,
    iterations: int,
    warmup_runs: int,
) -> tuple[list[float], list[float]]:
    for _ in range(warmup_runs):
        baseline_run_once()
        roar_run_once()

    baseline_samples: list[float] = []
    roar_samples: list[float] = []
    for _ in range(iterations):
        baseline_samples.append(baseline_run_once())
        roar_samples.append(roar_run_once())
    return baseline_samples, roar_samples


def _make_roar_runtime_env(*, job_id: str, cold_marker: str | None = None) -> dict:
    runtime_env = _prepare_worker_runtime_env({}, job_id)
    if cold_marker:
        marker_path = Path(runtime_env["working_dir"]) / ".ray_startup_cold_marker"
        marker_path.write_text(cold_marker, encoding="utf-8")
    return runtime_env


def main() -> int:
    args = parse_args()
    iterations = resolve_iterations(
        args.iterations,
        default_iterations=DEFAULT_ITERATIONS,
        quick_iterations=QUICK_ITERATIONS,
        quick=args.quick,
    )

    try:
        wait_for_cluster_readiness()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    def run_baseline_once() -> float:
        return _single_startup_run(runtime_env=None)

    def run_cold_once() -> float:
        runtime_env = _make_roar_runtime_env(
            job_id=f"startup-cold-{uuid.uuid4().hex[:8]}",
            cold_marker=uuid.uuid4().hex,
        )
        try:
            return _single_startup_run(runtime_env)
        finally:
            cleanup_runtime_env(runtime_env)

    baseline_for_cold_samples, cold_samples = _measure_runs_interleaved(
        run_baseline_once,
        run_cold_once,
        iterations=iterations,
        warmup_runs=WARMUP_RUNS,
    )

    warm_runtime_env = _make_roar_runtime_env(job_id="startup-warm")
    try:
        baseline_for_warm_samples, warm_samples = _measure_runs_interleaved(
            run_baseline_once,
            lambda: _single_startup_run(warm_runtime_env),
            iterations=iterations,
            warmup_runs=WARMUP_RUNS,
        )
    finally:
        cleanup_runtime_env(warm_runtime_env)

    baseline_samples = baseline_for_cold_samples + baseline_for_warm_samples
    baseline_mean = mean(baseline_samples)
    cold_baseline_mean = mean(baseline_for_cold_samples)
    warm_baseline_mean = mean(baseline_for_warm_samples)
    cold_mean = mean(cold_samples)
    warm_mean = mean(warm_samples)

    rows = [
        [
            "Baseline (no runtime_env)",
            f"{baseline_mean:.3f}",
            f"{stdev(baseline_samples):.3f}",
            "--",
            "--",
        ],
        [
            "With roar (cold)",
            f"{cold_mean:.3f}",
            f"{stdev(cold_samples):.3f}",
            f"{(cold_mean - cold_baseline_mean):+.3f}",
            f"{percent_delta(cold_mean, cold_baseline_mean):+.1f}%",
        ],
        [
            "With roar (warm cached)",
            f"{warm_mean:.3f}",
            f"{stdev(warm_samples):.3f}",
            f"{(warm_mean - warm_baseline_mean):+.3f}",
            f"{percent_delta(warm_mean, warm_baseline_mean):+.1f}%",
        ],
    ]

    print("\n--- Ray Job Startup Overhead ---")
    print(format_table(["Scenario", "Mean(s)", "Std(s)", "Delta(s)", "Delta%"], rows))

    payload = {
        **benchmark_metadata(ray_version=ray.__version__),
        "results": {
            "iterations": iterations,
            "warmup_runs": WARMUP_RUNS,
            "samples_seconds": {
                "baseline": baseline_samples,
                "baseline_cold": baseline_for_cold_samples,
                "baseline_warm": baseline_for_warm_samples,
                "roar_cold": cold_samples,
                "roar_warm": warm_samples,
            },
            "summary_seconds": {
                "baseline_mean": baseline_mean,
                "baseline_std": stdev(baseline_samples),
                "baseline_cold_mean": cold_baseline_mean,
                "baseline_cold_std": stdev(baseline_for_cold_samples),
                "baseline_warm_mean": warm_baseline_mean,
                "baseline_warm_std": stdev(baseline_for_warm_samples),
                "roar_cold_mean": cold_mean,
                "roar_cold_std": stdev(cold_samples),
                "roar_warm_mean": warm_mean,
                "roar_warm_std": stdev(warm_samples),
            },
            "overhead": {
                "cold_seconds": cold_mean - cold_baseline_mean,
                "cold_percent": percent_delta(cold_mean, cold_baseline_mean),
                "warm_seconds": warm_mean - warm_baseline_mean,
                "warm_percent": percent_delta(warm_mean, warm_baseline_mean),
            },
        },
    }

    result_path = write_results(RESULT_FILE, payload)
    print(f"\nSaved results to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
