#!/usr/bin/env python3
"""
Benchmark: hash algorithm throughput comparison.

Measures single-threaded throughput for blake3, sha256, sha512, and md5
at various file sizes, to populate the Hashes section of benchmarks.md.
"""

import hashlib
import os
import statistics
import tempfile
import time
from pathlib import Path

import blake3

ALGORITHMS = ["blake3", "sha256", "sha512", "md5"]
FILE_SIZES = [
    ("10 MB", 10 * 1024 * 1024),
    ("100 MB", 100 * 1024 * 1024),
    ("1 GB", 1024 * 1024 * 1024),
]
READ_CHUNK = 1 * 1024 * 1024  # 1 MiB chunks
REPETITIONS = 3
WARMUP = 1


def make_hasher(algo: str):
    if algo == "blake3":
        return blake3.blake3()
    return getattr(hashlib, algo)()


def hash_file(path: Path, algo: str) -> float:
    """Hash a file and return elapsed seconds."""
    h = make_hasher(algo)
    start = time.perf_counter()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(READ_CHUNK), b""):
            h.update(chunk)
    _ = h.hexdigest()
    return time.perf_counter() - start


def throughput_gbs(size_bytes: int, elapsed_s: float) -> float:
    return (size_bytes / (1024**3)) / elapsed_s if elapsed_s > 0 else 0.0


def main():
    print("Hash Algorithm Throughput Benchmark")
    print(f"  Algorithms: {', '.join(ALGORITHMS)}")
    print(f"  Repetitions: {REPETITIONS} (+ {WARMUP} warmup)")
    print(f"  Read chunk: {READ_CHUNK // 1024} KiB")
    print()

    # Results: {algo: {size_label: mean_gbs}}
    results: dict[str, dict[str, float]] = {a: {} for a in ALGORITHMS}

    with tempfile.TemporaryDirectory(prefix="hash_bench_") as tmpdir:
        for size_label, size_bytes in FILE_SIZES:
            # Generate test file
            path = Path(tmpdir) / f"test_{size_bytes}.bin"
            print(f"Generating {size_label} test file...", end=" ", flush=True)
            with open(path, "wb") as f:
                remaining = size_bytes
                chunk = os.urandom(min(READ_CHUNK, remaining))
                while remaining > 0:
                    write_size = min(len(chunk), remaining)
                    f.write(chunk[:write_size])
                    remaining -= write_size
            print("done")

            # Warm the page cache
            with open(path, "rb") as f:
                while f.read(READ_CHUNK):
                    pass

            for algo in ALGORITHMS:
                # Warmup
                for _ in range(WARMUP):
                    hash_file(path, algo)

                # Measure
                times = []
                for _ in range(REPETITIONS):
                    # Re-warm page cache between algorithms
                    elapsed = hash_file(path, algo)
                    times.append(elapsed)

                mean_s = statistics.mean(times)
                gbs = throughput_gbs(size_bytes, mean_s)
                results[algo][size_label] = gbs

                print(f"  {algo:>6} x {size_label:>6}: {mean_s:.3f}s mean  ({gbs:.2f} GB/s)")

            # Clean up large file before next size
            path.unlink()
            print()

    # Summary table: single-threaded throughput
    print("=" * 60)
    print("Single-threaded throughput (GB/s, page-cached)")
    print("=" * 60)
    header = f"{'Algorithm':>10}"
    for size_label, _ in FILE_SIZES:
        header += f"  {size_label:>10}"
    print(header)
    print("-" * len(header))
    for algo in ALGORITHMS:
        row = f"{algo:>10}"
        for size_label, _ in FILE_SIZES:
            gbs = results[algo].get(size_label, 0)
            row += f"  {gbs:>9.2f}"
        print(row)

    # Wall-clock cost table
    print()
    print("=" * 60)
    print("Wall-clock hashing cost (seconds)")
    print("=" * 60)
    header = f"{'File size':>10}"
    for algo in ALGORITHMS:
        header += f"  {algo:>10}"
    print(header)
    print("-" * len(header))
    for size_label, size_bytes in FILE_SIZES:
        row = f"{size_label:>10}"
        for algo in ALGORITHMS:
            gbs = results[algo].get(size_label, 0)
            if gbs > 0:
                secs = (size_bytes / (1024**3)) / gbs
                row += f"  {secs:>10.3f}"
            else:
                row += f"  {'N/A':>10}"
        print(row)


if __name__ == "__main__":
    main()
