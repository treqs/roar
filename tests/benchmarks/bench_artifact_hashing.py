#!/usr/bin/env python3
"""
Benchmark artifact hashing performance: Python baseline vs native Rust backend.

This benchmark intentionally keeps both paths in one script:
1. Baseline: pure Python (blake3/hashlib), one-pass multi-hash per file
2. Native: roar.db.hashing.backend via roar._hash_native (Rust)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import blake3

from roar.db.hashing import backend

READ_CHUNK_SIZE = 8 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark artifact hashing backends")
    parser.add_argument("--small-files", type=int, default=600, help="Count of small files")
    parser.add_argument("--small-bytes", type=int, default=8 * 1024, help="Size of small files")
    parser.add_argument("--large-files", type=int, default=8, help="Count of large files")
    parser.add_argument(
        "--large-bytes", type=int, default=8 * 1024 * 1024, help="Size of large files"
    )
    parser.add_argument("--repetitions", type=int, default=5, help="Benchmark repetitions")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup runs")
    parser.add_argument(
        "--algorithms",
        nargs="+",
        default=["blake3", "sha256"],
        help="Algorithms to compute",
    )
    parser.add_argument("--workers", type=int, default=None, help="Optional native worker count")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON output path")
    return parser.parse_args()


def _generate_files(
    root: Path, small_files: int, small_bytes: int, large_files: int, large_bytes: int
) -> list[Path]:
    rng = random.Random(7)
    files: list[Path] = []

    for idx in range(small_files):
        path = root / f"small_{idx:05d}.bin"
        path.write_bytes(rng.randbytes(small_bytes))
        files.append(path)

    for idx in range(large_files):
        path = root / f"large_{idx:03d}.bin"
        path.write_bytes(rng.randbytes(large_bytes))
        files.append(path)

    return files


def _python_hash_file(path: Path, algorithms: list[str]) -> dict[str, str]:
    hashers: dict[str, Any] = {}
    for algorithm in algorithms:
        if algorithm == "blake3":
            hashers[algorithm] = blake3.blake3()
        elif algorithm == "sha256":
            hashers[algorithm] = hashlib.sha256()
        elif algorithm == "sha512":
            hashers[algorithm] = hashlib.sha512()
        elif algorithm == "md5":
            hashers[algorithm] = hashlib.md5()
        else:
            raise ValueError(f"Unsupported algorithm in benchmark: {algorithm}")

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(READ_CHUNK_SIZE), b""):
            for hasher in hashers.values():
                hasher.update(chunk)

    return {algorithm: hasher.hexdigest() for algorithm, hasher in hashers.items()}


def python_baseline(files: list[Path], algorithms: list[str]) -> dict[str, dict[str, str]]:
    return {str(path): _python_hash_file(path, algorithms) for path in files}


def native_backend(
    files: list[Path], algorithms: list[str], workers: int | None
) -> dict[str, dict[str, str]]:
    return backend.compute_hashes_batch(files, algorithms, workers)


def timed(fn, *args, **kwargs) -> tuple[float, Any]:
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - start, result


def summarize_times(times: list[float]) -> dict[str, float]:
    return {
        "mean_seconds": statistics.mean(times),
        "median_seconds": statistics.median(times),
        "min_seconds": min(times),
        "max_seconds": max(times),
        "stdev_seconds": statistics.stdev(times) if len(times) > 1 else 0.0,
    }


def _print_summary(
    label: str,
    summary: dict[str, float],
    total_bytes: int,
    total_files: int,
) -> None:
    mean = summary["mean_seconds"]
    mb_per_s = (total_bytes / (1024 * 1024)) / mean
    files_per_s = total_files / mean
    print(
        f"{label:>10} | mean={mean:8.3f}s | median={summary['median_seconds']:8.3f}s | "
        f"files/s={files_per_s:10.1f} | MiB/s={mb_per_s:10.1f}"
    )


def main() -> int:
    args = parse_args()
    algorithms = backend.normalize_algorithms(args.algorithms)

    native_available = backend._hash_native is not None
    if not native_available:
        print("Native module not loaded (`roar._hash_native`).")
        print("Build it first with:")
        print("  ./scripts/build_hash_native.sh")
        return 2

    with tempfile.TemporaryDirectory(prefix="roar_hash_bench_") as temp_dir:
        root = Path(temp_dir)
        files = _generate_files(
            root=root,
            small_files=args.small_files,
            small_bytes=args.small_bytes,
            large_files=args.large_files,
            large_bytes=args.large_bytes,
        )

        total_bytes = sum(path.stat().st_size for path in files)
        total_files = len(files)
        print("Artifact Hashing Benchmark")
        print(f"  Files: {total_files}")
        print(f"  Bytes: {total_bytes} ({total_bytes / (1024 * 1024):.1f} MiB)")
        print(f"  Algorithms: {', '.join(algorithms)}")
        if args.workers is not None:
            print(f"  Native workers: {args.workers}")

        # Warmup both paths.
        for _ in range(args.warmup):
            python_baseline(files, algorithms)
            native_backend(files, algorithms, args.workers)

        python_times: list[float] = []
        native_times: list[float] = []
        baseline_result: dict[str, dict[str, str]] | None = None
        native_result: dict[str, dict[str, str]] | None = None

        for _ in range(args.repetitions):
            elapsed, baseline_result = timed(python_baseline, files, algorithms)
            python_times.append(elapsed)

            elapsed, native_result = timed(native_backend, files, algorithms, args.workers)
            native_times.append(elapsed)

        assert baseline_result is not None
        assert native_result is not None
        if baseline_result != native_result:
            raise RuntimeError("Digest mismatch between baseline and native results")

        python_summary = summarize_times(python_times)
        native_summary = summarize_times(native_times)

        print("")
        _print_summary("python", python_summary, total_bytes, total_files)
        _print_summary("native", native_summary, total_bytes, total_files)
        speedup = python_summary["mean_seconds"] / native_summary["mean_seconds"]
        print(f"{'speedup':>10} | {speedup:8.2f}x (mean)")

        report = {
            "timestamp": time.time(),
            "cwd": os.getcwd(),
            "dataset": {
                "small_files": args.small_files,
                "small_bytes": args.small_bytes,
                "large_files": args.large_files,
                "large_bytes": args.large_bytes,
                "total_files": total_files,
                "total_bytes": total_bytes,
            },
            "algorithms": algorithms,
            "workers": args.workers,
            "python": python_summary,
            "native": native_summary,
            "speedup_mean": speedup,
        }

        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(report, indent=2))
            print(f"Saved benchmark report to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
