#!/usr/bin/env python3
"""
Benchmark script for roar-tracer overhead measurement.

This script creates various workloads to test the ptrace-based syscall
tracing overhead of roar-tracer across different I/O patterns.
"""

import os
import sys
import subprocess
import tempfile
import shutil
import time
import statistics
from pathlib import Path
from typing import List, Tuple, Dict

# Configuration
PYTHON_BIN = "/home/trevor/dev/roar/.venv/bin/python"
TRACER_BIN = "/home/trevor/dev/roar/roar/bin/roar-tracer"
NUM_ITERATIONS = 10
NUM_WARMUP = 1

# Workload scripts
WORKLOAD_SCRIPTS = {
    "cpu_bound": """
# CPU-bound workload - pure computation with minimal syscalls
def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True

# Sum all primes up to 50000
total = sum(i for i in range(2, 50000) if is_prime(i))
print(f"Sum of primes: {total}")
""",

    "io_read_heavy": """
# I/O read-heavy workload
import os
import tempfile
import shutil

# Create temp directory and files
workdir = tempfile.mkdtemp(prefix="bench_read_")
try:
    # Create 200 small files (1KB each)
    for i in range(200):
        with open(os.path.join(workdir, f"file_{i}.txt"), "w") as f:
            f.write("x" * 1024)

    # Read all files
    total_bytes = 0
    for i in range(200):
        with open(os.path.join(workdir, f"file_{i}.txt"), "r") as f:
            total_bytes += len(f.read())

    print(f"Read {total_bytes} bytes from 200 files")
finally:
    shutil.rmtree(workdir)
""",

    "io_write_heavy": """
# I/O write-heavy workload
import os
import tempfile
import shutil

# Create temp directory
workdir = tempfile.mkdtemp(prefix="bench_write_")
try:
    # Write 200 small files (1KB each)
    total_bytes = 0
    for i in range(200):
        data = f"File {i} content " * 64  # ~1KB
        with open(os.path.join(workdir, f"output_{i}.txt"), "w") as f:
            f.write(data)
            total_bytes += len(data)

    print(f"Wrote {total_bytes} bytes to 200 files")
finally:
    shutil.rmtree(workdir)
""",

    "syscall_heavy": """
# Syscall-heavy workload - rapid open/stat/close cycles
import os
import tempfile
import shutil

# Create temp directory and files
workdir = tempfile.mkdtemp(prefix="bench_syscall_")
try:
    # Create 500 files
    for i in range(500):
        path = os.path.join(workdir, f"file_{i}.txt")
        with open(path, "w") as f:
            f.write("test")

    # Rapidly open/stat/close each file
    count = 0
    for i in range(500):
        path = os.path.join(workdir, f"file_{i}.txt")
        # Open and immediately close
        with open(path, "r") as f:
            pass
        # Stat the file
        os.stat(path)
        count += 1

    print(f"Performed syscalls on {count} files")
finally:
    shutil.rmtree(workdir)
""",

    "process_spawn": """
# Process spawn workload - fork/exec overhead
import subprocess

count = 0
for i in range(20):
    # Spawn a simple process
    subprocess.run(["true"], capture_output=True)
    count += 1

print(f"Spawned {count} processes")
""",

    "mixed_realistic": """
# Mixed realistic workload - simulates ML pipeline step
import os
import tempfile
import shutil
import json

workdir = tempfile.mkdtemp(prefix="bench_mixed_")
try:
    # Step 1: Read 5 input files (config/data)
    input_data = []
    for i in range(5):
        path = os.path.join(workdir, f"input_{i}.json")
        data = {"id": i, "values": list(range(100))}
        with open(path, "w") as f:
            json.dump(data, f)

    for i in range(5):
        path = os.path.join(workdir, f"input_{i}.json")
        with open(path, "r") as f:
            input_data.append(json.load(f))

    # Step 2: Computation (simple data processing)
    results = []
    for data in input_data:
        total = sum(data["values"])
        results.append({"id": data["id"], "sum": total, "mean": total / len(data["values"])})

    # Step 3: Write 5 output files
    for i, result in enumerate(results):
        path = os.path.join(workdir, f"output_{i}.json")
        with open(path, "w") as f:
            json.dump(result, f)

    print(f"Processed {len(input_data)} inputs, wrote {len(results)} outputs")
finally:
    shutil.rmtree(workdir)
"""
}


def create_workload_script(workdir: Path, name: str, code: str) -> Path:
    """Create a workload script file."""
    script_path = workdir / f"{name}.py"
    script_path.write_text(code)
    return script_path


def run_command(cmd: List[str], timeout: int = 60) -> Tuple[float, bool]:
    """
    Run a command and measure wall time.
    Returns (elapsed_time, success).
    """
    start = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True
        )
        elapsed = time.perf_counter() - start
        return elapsed, result.returncode == 0
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        return elapsed, False
    except Exception as e:
        print(f"Error running command: {e}", file=sys.stderr)
        return 0.0, False


def benchmark_workload(
    workload_name: str,
    script_path: Path,
    num_iterations: int,
    warmup: int = 1
) -> Tuple[List[float], List[float]]:
    """
    Benchmark a workload both with and without tracer.
    Returns (baseline_times, traced_times).
    """
    baseline_times = []
    traced_times = []

    # Warmup runs
    print(f"  Warming up ({warmup} iteration)...", end=" ", flush=True)
    for _ in range(warmup):
        run_command([PYTHON_BIN, str(script_path)])
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as trace_out:
            run_command([TRACER_BIN, trace_out.name, PYTHON_BIN, str(script_path)])
    print("done")

    # Baseline runs
    print(f"  Running baseline ({num_iterations} iterations)...", end=" ", flush=True)
    for i in range(num_iterations):
        elapsed, success = run_command([PYTHON_BIN, str(script_path)])
        if success:
            baseline_times.append(elapsed)
        else:
            print(f"\n  Warning: baseline iteration {i+1} failed", file=sys.stderr)
    print("done")

    # Traced runs
    print(f"  Running traced ({num_iterations} iterations)...", end=" ", flush=True)
    for i in range(num_iterations):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as trace_out:
            elapsed, success = run_command([TRACER_BIN, trace_out.name, PYTHON_BIN, str(script_path)])
            if success:
                traced_times.append(elapsed)
            else:
                print(f"\n  Warning: traced iteration {i+1} failed", file=sys.stderr)
    print("done")

    return baseline_times, traced_times


def calculate_stats(times: List[float]) -> Tuple[float, float]:
    """Calculate mean and standard deviation."""
    if not times:
        return 0.0, 0.0
    mean = statistics.mean(times)
    stddev = statistics.stdev(times) if len(times) > 1 else 0.0
    return mean, stddev


def main():
    print("=" * 80)
    print("roar-tracer Performance Benchmark")
    print("=" * 80)
    print(f"Tracer binary: {TRACER_BIN}")
    print(f"Python interpreter: {PYTHON_BIN}")
    print(f"Iterations per workload: {NUM_ITERATIONS}")
    print(f"Warmup iterations: {NUM_WARMUP}")
    print("=" * 80)
    print()

    # Verify binaries exist
    if not os.path.exists(TRACER_BIN):
        print(f"Error: Tracer binary not found at {TRACER_BIN}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(PYTHON_BIN):
        print(f"Error: Python interpreter not found at {PYTHON_BIN}", file=sys.stderr)
        sys.exit(1)

    # Create temporary directory for workload scripts
    with tempfile.TemporaryDirectory(prefix="bench_tracer_") as tmpdir:
        workdir = Path(tmpdir)

        # Results storage
        results = {}

        # Run benchmarks for each workload
        for workload_name in WORKLOAD_SCRIPTS.keys():
            print(f"Benchmarking: {workload_name}")

            # Create workload script
            script_path = create_workload_script(
                workdir,
                workload_name,
                WORKLOAD_SCRIPTS[workload_name]
            )

            # Run benchmark
            baseline_times, traced_times = benchmark_workload(
                workload_name,
                script_path,
                NUM_ITERATIONS,
                NUM_WARMUP
            )

            # Calculate statistics
            baseline_mean, baseline_std = calculate_stats(baseline_times)
            traced_mean, traced_std = calculate_stats(traced_times)

            # Calculate overhead percentage
            if baseline_mean > 0:
                overhead_pct = ((traced_mean - baseline_mean) / baseline_mean) * 100
            else:
                overhead_pct = 0.0

            results[workload_name] = {
                "baseline_mean": baseline_mean,
                "baseline_std": baseline_std,
                "traced_mean": traced_mean,
                "traced_std": traced_std,
                "overhead_pct": overhead_pct,
                "baseline_times": baseline_times,
                "traced_times": traced_times
            }

            print()

    # Print results table
    print("=" * 80)
    print("RESULTS")
    print("=" * 80)
    print()
    print(f"{'Workload':<20} | {'Baseline (s)':<15} | {'Traced (s)':<15} | {'Overhead %':<12} | {'Stddev B':<10} | {'Stddev T':<10}")
    print("-" * 130)

    total_overhead = 0.0
    count = 0

    for workload_name, data in results.items():
        baseline_mean = data["baseline_mean"]
        baseline_std = data["baseline_std"]
        traced_mean = data["traced_mean"]
        traced_std = data["traced_std"]
        overhead_pct = data["overhead_pct"]

        print(f"{workload_name:<20} | "
              f"{baseline_mean:>6.3f}±{baseline_std:<6.3f} | "
              f"{traced_mean:>6.3f}±{traced_std:<6.3f} | "
              f"{overhead_pct:>+10.1f}% | "
              f"{baseline_std:>10.3f} | "
              f"{traced_std:>10.3f}")

        total_overhead += overhead_pct
        count += 1

    print("-" * 130)
    avg_overhead = total_overhead / count if count > 0 else 0.0
    print(f"\nAverage overhead across all workloads: {avg_overhead:+.1f}%")
    print()

    # Print detailed statistics
    print("=" * 80)
    print("DETAILED STATISTICS")
    print("=" * 80)
    print()

    for workload_name, data in results.items():
        print(f"{workload_name}:")
        print(f"  Baseline: n={len(data['baseline_times'])}, "
              f"mean={data['baseline_mean']:.4f}s, "
              f"std={data['baseline_std']:.4f}s, "
              f"min={min(data['baseline_times']):.4f}s, "
              f"max={max(data['baseline_times']):.4f}s")
        print(f"  Traced:   n={len(data['traced_times'])}, "
              f"mean={data['traced_mean']:.4f}s, "
              f"std={data['traced_std']:.4f}s, "
              f"min={min(data['traced_times']):.4f}s, "
              f"max={max(data['traced_times']):.4f}s")
        print(f"  Overhead: {data['overhead_pct']:+.2f}%")
        print()


if __name__ == "__main__":
    main()
