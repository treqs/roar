from __future__ import annotations

import json
import statistics
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "tests" / "benchmarks" / "results"
COMPOSE_FILE = REPO_ROOT / "tests" / "e2e" / "ray" / "docker-compose.yml"
RAY_ADDRESS = "ray://localhost:10001"
MINIO_ENDPOINT = "http://localhost:9000"
MINIO_INTERNAL_ENDPOINT = "http://minio:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"


@dataclass(frozen=True)
class ClusterStatus:
    running_services: set[str]
    expected_services: set[str]


def run_command(args: Sequence[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        text=True,
        capture_output=True,
    )


def ensure_ray_docker_cluster_running() -> ClusterStatus:
    expected = {"ray-head", "ray-worker-1", "ray-worker-2", "minio"}
    running_proc = run_command(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "ps",
            "--services",
            "--status",
            "running",
        ]
    )
    running_services = {line.strip() for line in running_proc.stdout.splitlines() if line.strip()}

    if not expected.issubset(running_services):
        missing = sorted(expected - running_services)
        running = sorted(running_services)
        raise RuntimeError(
            "Ray Docker cluster is not fully running (checked via `docker compose ps`).\n"
            f"  Missing services: {', '.join(missing) if missing else 'none'}\n"
            f"  Running services: {', '.join(running) if running else 'none'}\n"
            f"  Start it with: docker compose -f {COMPOSE_FILE} up -d --build"
        )

    return ClusterStatus(running_services=running_services, expected_services=expected)


def get_git_commit() -> str:
    proc = run_command(["git", "rev-parse", "HEAD"])
    if proc.returncode == 0:
        return proc.stdout.strip()
    return "unknown"


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def benchmark_metadata(*, ray_version: str) -> dict[str, Any]:
    return {
        "timestamp": now_iso_utc(),
        "git_commit": get_git_commit(),
        "ray_version": ray_version,
        "python_version": sys.version.split()[0],
    }


def write_results(filename: str, payload: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    target = RESULTS_DIR / filename
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def resolve_iterations(
    iterations_flag: int | None,
    *,
    default_iterations: int,
    quick_iterations: int,
    quick: bool,
) -> int:
    if iterations_flag is not None:
        return iterations_flag
    return quick_iterations if quick else default_iterations


def mean(values: Iterable[float]) -> float:
    data = list(values)
    if not data:
        return 0.0
    return statistics.fmean(data)


def stdev(values: Iterable[float]) -> float:
    data = list(values)
    if len(data) < 2:
        return 0.0
    return statistics.stdev(data)


def linear_regression(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have the same length")
    n = len(xs)
    if n == 0:
        return 0.0, 0.0

    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    denom = n * sxx - sx * sx
    if denom == 0:
        return sy / n, 0.0

    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return intercept, slope


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def _fmt(values: Sequence[str]) -> str:
        return "  ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    parts = [_fmt(headers), _fmt(["-" * width for width in widths])]
    parts.extend(_fmt(row) for row in rows)
    return "\n".join(parts)


def minio_client(endpoint_url: str):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )


def ensure_minio_bucket(bucket_name: str, endpoint_url: str = MINIO_ENDPOINT) -> None:
    client = minio_client(endpoint_url)
    try:
        client.head_bucket(Bucket=bucket_name)
    except Exception:
        client.create_bucket(Bucket=bucket_name)


def percent_delta(new_value: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0
    return ((new_value - baseline) / baseline) * 100.0


def cleanup_runtime_env(runtime_env: dict[str, Any] | None) -> None:
    if not runtime_env:
        return
    working_dir = runtime_env.get("working_dir")
    if not isinstance(working_dir, str) or not working_dir.strip():
        return
    path = Path(working_dir)
    if path.exists():
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def wait_for_cluster_readiness(timeout_seconds: float = 60.0) -> None:
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            ensure_ray_docker_cluster_running()
            return
        except RuntimeError:
            time.sleep(2.0)
    ensure_ray_docker_cluster_running()
