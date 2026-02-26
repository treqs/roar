#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from collections import defaultdict
from dataclasses import dataclass

import ray

from roar.ray.actor import RoarLogCollectorActor
from tests.benchmarks.ray_bench_utils import (
    RAY_ADDRESS,
    benchmark_metadata,
    ensure_ray_docker_cluster_running,
    format_table,
    mean,
    resolve_iterations,
    stdev,
    write_results,
)

DEFAULT_ITERATIONS = 3
QUICK_ITERATIONS = 2
DURATION_SECONDS = 10.0
WARMUP_DURATION_SECONDS = 2.0
RESULT_FILE = "ray_actor_ipc_latest.json"

FULL_BATCH_SIZES = [1, 10, 50, 100]
FULL_WORKER_COUNTS = [1, 2, 4, 8, 16]
QUICK_BATCH_SIZES = [1, 50]
QUICK_WORKER_COUNTS = [1, 4, 8]


@dataclass
class TrialResult:
    throughput_events_per_second: float
    latency_ms_per_batch: float
    total_events: int
    total_batches: int
    elapsed_seconds: float


@ray.remote
def _hammer_actor(actor, batch_size: int, duration_seconds: float) -> dict[str, float | int]:
    batch = [
        {
            "path": "/tmp/bench",
            "mode": "r",
            "task_id": "bench-task",
            "ts": time.time(),
        }
    ] * batch_size

    total_events = 0
    total_batches = 0
    latency_sum_s = 0.0
    deadline = time.perf_counter() + duration_seconds

    while time.perf_counter() < deadline:
        start = time.perf_counter()
        ray.get(actor.append_batch.remote(batch))
        latency_sum_s += time.perf_counter() - start
        total_batches += 1
        total_events += batch_size

    return {
        "events": total_events,
        "batches": total_batches,
        "latency_sum_s": latency_sum_s,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark RoarLogCollectorActor throughput")
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help=f"Measurement iterations (default: {DEFAULT_ITERATIONS}, quick: {QUICK_ITERATIONS})",
    )
    parser.add_argument("--quick", action="store_true", help="Use reduced batch/worker sweep")
    parser.add_argument(
        "--duration",
        type=float,
        default=DURATION_SECONDS,
        help=f"Load duration per trial in seconds (default: {DURATION_SECONDS})",
    )
    return parser.parse_args()


def _run_trial(batch_size: int, n_workers: int, duration_seconds: float) -> TrialResult:
    actor = RoarLogCollectorActor.options(num_cpus=0).remote()
    try:
        start = time.perf_counter()
        refs = [_hammer_actor.remote(actor, batch_size, duration_seconds) for _ in range(n_workers)]
        worker_results = ray.get(refs)
        elapsed = time.perf_counter() - start
    finally:
        ray.kill(actor, no_restart=True)

    total_events = int(sum(int(item["events"]) for item in worker_results))
    total_batches = int(sum(int(item["batches"]) for item in worker_results))
    latency_sum_s = float(sum(float(item["latency_sum_s"]) for item in worker_results))

    throughput = total_events / elapsed if elapsed else 0.0
    latency_ms = (latency_sum_s / total_batches * 1000.0) if total_batches else 0.0
    return TrialResult(
        throughput_events_per_second=throughput,
        latency_ms_per_batch=latency_ms,
        total_events=total_events,
        total_batches=total_batches,
        elapsed_seconds=elapsed,
    )


def _find_saturation(results: list[dict]) -> dict[str, float | int | None]:
    if not results:
        return {
            "batch_size": None,
            "workers": None,
            "throughput_events_per_second": None,
            "threshold_gain": 1.10,
        }

    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in results:
        grouped[int(row["batch_size"])].append(row)

    best_batch = max(
        grouped,
        key=lambda batch: max(item["throughput_events_per_second"] for item in grouped[batch]),
    )
    sorted_rows = sorted(grouped[best_batch], key=lambda item: int(item["workers"]))

    prev = None
    chosen = sorted_rows[-1]
    for row in sorted_rows:
        current = float(row["throughput_events_per_second"])
        if prev is not None and prev > 0 and (current / prev) <= 1.10:
            chosen = row
            break
        prev = current

    return {
        "batch_size": int(chosen["batch_size"]),
        "workers": int(chosen["workers"]),
        "throughput_events_per_second": float(chosen["throughput_events_per_second"]),
        "threshold_gain": 1.10,
    }


def main() -> int:
    args = parse_args()
    iterations = resolve_iterations(
        args.iterations,
        default_iterations=DEFAULT_ITERATIONS,
        quick_iterations=QUICK_ITERATIONS,
        quick=args.quick,
    )

    batch_sizes = QUICK_BATCH_SIZES if args.quick else FULL_BATCH_SIZES
    worker_counts = QUICK_WORKER_COUNTS if args.quick else FULL_WORKER_COUNTS

    try:
        ensure_ray_docker_cluster_running()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 2

    ray.init(address=RAY_ADDRESS, ignore_reinit_error=False, logging_level="ERROR")
    aggregated_rows: list[dict] = []

    try:
        for batch_size in batch_sizes:
            for n_workers in worker_counts:
                _run_trial(batch_size, n_workers, min(WARMUP_DURATION_SECONDS, args.duration))

                trial_results = [
                    _run_trial(batch_size, n_workers, args.duration) for _ in range(iterations)
                ]

                throughputs = [item.throughput_events_per_second for item in trial_results]
                latencies = [item.latency_ms_per_batch for item in trial_results]

                aggregated_rows.append(
                    {
                        "batch_size": batch_size,
                        "workers": n_workers,
                        "throughput_events_per_second": mean(throughputs),
                        "throughput_std": stdev(throughputs),
                        "latency_ms_per_batch": mean(latencies),
                        "latency_std": stdev(latencies),
                        "iterations": [
                            {
                                "throughput_events_per_second": item.throughput_events_per_second,
                                "latency_ms_per_batch": item.latency_ms_per_batch,
                                "total_events": item.total_events,
                                "total_batches": item.total_batches,
                                "elapsed_seconds": item.elapsed_seconds,
                            }
                            for item in trial_results
                        ],
                    }
                )
    finally:
        ray.shutdown()

    saturation = _find_saturation(aggregated_rows)

    table_rows = [
        [
            str(row["batch_size"]),
            str(row["workers"]),
            f"{row['throughput_events_per_second']:.0f}",
            f"{row['latency_ms_per_batch']:.3f}",
            f"{row['throughput_std']:.0f}",
        ]
        for row in aggregated_rows
    ]

    print("\n--- Ray Actor IPC Throughput ---")
    print(
        format_table(
            ["Batch", "Workers", "Events/sec", "Latency/batch(ms)", "Std events/sec"],
            table_rows,
        )
    )
    print(
        "\nSaturation point: "
        f"batch_size={saturation['batch_size']}, workers={saturation['workers']}, "
        f"throughput={saturation['throughput_events_per_second']:.0f} events/sec"
    )

    payload = {
        **benchmark_metadata(ray_version=ray.__version__),
        "results": {
            "iterations": iterations,
            "duration_seconds": args.duration,
            "warmup_duration_seconds": min(WARMUP_DURATION_SECONDS, args.duration),
            "batch_sizes": batch_sizes,
            "worker_counts": worker_counts,
            "rows": aggregated_rows,
            "saturation": saturation,
        },
    }

    result_path = write_results(RESULT_FILE, payload)
    print(f"\nSaved results to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
