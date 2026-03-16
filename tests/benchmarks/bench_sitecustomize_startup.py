#!/usr/bin/env python3
"""
Benchmark: sitecustomize.py startup overhead.

Measures Python process startup time with and without ROAR_WRAP=1,
broken down by: sitecustomize import, _write_log atexit, _collect_ray_io atexit.

Usage: python tests/benchmarks/bench_sitecustomize_startup.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

INJECT_DIR = Path(__file__).resolve().parents[2] / "roar" / "execution" / "runtime" / "inject"
N = 10


def _roar_env(log_file=None):
    env = {**os.environ, "ROAR_WRAP": "1"}
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(INJECT_DIR) if not existing else str(INJECT_DIR) + os.pathsep + existing
    if log_file:
        env["ROAR_LOG_FILE"] = log_file
    else:
        env.pop("ROAR_LOG_FILE", None)
    return env


def timed(env, n=N, log_file=None):
    if log_file:
        env = {**env, "ROAR_LOG_FILE": log_file}
    times = []
    for _ in range(n):
        t = time.perf_counter()
        subprocess.run(
            [sys.executable, "-c", "pass"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        times.append((time.perf_counter() - t) * 1000)
    return sum(times) / len(times)


def main():
    import tempfile

    print(f"Measuring startup (N={N} runs each)...")

    env_base = {**os.environ, "ROAR_WRAP": "0"}
    env_roar = _roar_env()

    baseline = timed(env_base)
    print(f"  baseline:                    {baseline:.1f}ms")

    roar_no_log = timed(env_roar)
    print(f"  ROAR_WRAP=1 (no LOG_FILE):   {roar_no_log:.1f}ms  (+{roar_no_log - baseline:.1f}ms)")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        log_path = handle.name
    roar_with_log = timed(env_roar, log_file=log_path)
    print(
        f"  ROAR_WRAP=1 + LOG_FILE:      {roar_with_log:.1f}ms  (+{roar_with_log - baseline:.1f}ms)"
    )
    os.unlink(log_path)

    print()
    print(f"  sitecustomize import cost:   {roar_no_log - baseline:.1f}ms")
    print(f"  atexit handlers cost:        {roar_with_log - roar_no_log:.1f}ms")
    print(f"  total ROAR_WRAP=1 overhead:  {roar_with_log - baseline:.1f}ms")


if __name__ == "__main__":
    main()
