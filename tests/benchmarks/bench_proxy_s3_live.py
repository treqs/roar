#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import random
import re
import socket
import statistics
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

FILE_SIZES = {
    "small": 64 * 1024,
    "medium": 1024 * 1024,
    "large": 8 * 1024 * 1024,
}

TIMING_RE = re.compile(r"^\[proxy-timing\] (?P<label>[a-z_]+)=(?P<ms>\d+\.\d+)ms$")


@dataclass
class ProxyProcess:
    process: subprocess.Popen[str]
    port: int
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None


@dataclass
class CaseResult:
    direct_samples: list[float] = field(default_factory=list)
    proxy_samples: list[float] = field(default_factory=list)
    paired_deltas: list[float] = field(default_factory=list)
    direct_first_count: int = 0
    proxy_first_count: int = 0


@dataclass(frozen=True)
class BenchmarkCase:
    label: str
    operation_name: str
    direct_operation: Callable[[], None]
    proxy_operation: Callable[[], None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark roar-proxy against live S3 from the local machine."
    )
    parser.add_argument(
        "--bucket",
        help="S3 bucket to use. If omitted with --create-bucket, a unique bucket is created.",
    )
    parser.add_argument(
        "--create-bucket",
        action="store_true",
        help="Create the benchmark bucket if it does not already exist.",
    )
    parser.add_argument(
        "--keep-bucket",
        action="store_true",
        help="Keep the auto-created bucket instead of deleting it.",
    )
    parser.add_argument(
        "--binary",
        default=None,
        help="Path to roar-proxy binary. Defaults to the local release build if present.",
    )
    parser.add_argument(
        "--iterations", type=int, default=5, help="Measurement iterations per case."
    )
    parser.add_argument("--warmups", type=int, default=2, help="Warmup requests per case.")
    parser.add_argument(
        "--range-ratio",
        type=float,
        default=0.25,
        help="Range GET size as a fraction of the object size.",
    )
    parser.add_argument("--range-min-bytes", type=int, default=16 * 1024)
    parser.add_argument("--range-cap-bytes", type=int, default=1024 * 1024)
    parser.add_argument(
        "--prefix", default=None, help="Object key prefix. Defaults to a unique run prefix."
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=12345,
        help="Random seed for paired request ordering.",
    )
    parser.add_argument(
        "--buffer-bytes",
        type=int,
        default=None,
        help="Set ROAR_PROXY_BUFFER_RESPONSE_BYTES for the proxy process.",
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="Enable ROAR_PROXY_TIMING=1 and summarize proxy stderr timing output.",
    )
    return parser.parse_args()


def resolve_binary(explicit: str | None) -> str:
    if explicit:
        return explicit

    repo_root = Path(__file__).resolve().parents[2]
    release_binary = repo_root / "rust" / "target" / "release" / "roar-proxy"
    if release_binary.exists():
        return str(release_binary)

    packaged_binary = repo_root / "roar" / "bin" / "roar-proxy"
    if packaged_binary.exists():
        return str(packaged_binary)

    return "roar-proxy"


def boto3_client(endpoint_url: str | None = None):
    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    return boto3.client("s3", endpoint_url=endpoint_url, region_name=region)


def ensure_bucket(client, bucket: str, create_bucket: bool) -> None:
    try:
        client.head_bucket(Bucket=bucket)
        return
    except ClientError:
        if not create_bucket:
            raise

    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    kwargs: dict[str, object] = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**kwargs)


def delete_bucket_contents(client, bucket: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})


def maybe_cleanup_bucket(client, bucket: str, keep_bucket: bool, auto_created: bool) -> None:
    if keep_bucket or not auto_created:
        return
    delete_bucket_contents(client, bucket)
    client.delete_bucket(Bucket=bucket)


def payload(size_bytes: int) -> bytes:
    block = hashlib_seed(size_bytes)
    repeats = (size_bytes // len(block)) + 1
    return (block * repeats)[:size_bytes]


def hashlib_seed(size_bytes: int) -> bytes:
    import hashlib

    return hashlib.sha256(f"roar-proxy-live:{size_bytes}".encode()).digest()


def read_body(response: dict) -> None:
    body = response["Body"]
    try:
        body.read()
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


def range_header(size_bytes: int, ratio: float, minimum: int, cap: int) -> str:
    wanted = int(size_bytes * ratio)
    wanted = max(minimum, min(size_bytes, cap, wanted))
    return f"bytes=0-{wanted - 1}"


def measure(operation, iterations: int, warmups: int) -> list[float]:
    for _ in range(warmups):
        operation()

    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def mean_ms(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def median_ms(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def percent_delta(candidate: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0
    return ((candidate - baseline) / baseline) * 100.0


def timed(operation: Callable[[], None]) -> float:
    start = time.perf_counter()
    operation()
    return (time.perf_counter() - start) * 1000.0


def run_paired_round(
    case: BenchmarkCase, *, direct_first: bool, result: CaseResult | None = None
) -> None:
    if direct_first:
        direct_ms = timed(case.direct_operation)
        proxy_ms = timed(case.proxy_operation)
    else:
        proxy_ms = timed(case.proxy_operation)
        direct_ms = timed(case.direct_operation)

    if result is None:
        return

    result.direct_samples.append(direct_ms)
    result.proxy_samples.append(proxy_ms)
    result.paired_deltas.append(proxy_ms - direct_ms)
    if direct_first:
        result.direct_first_count += 1
    else:
        result.proxy_first_count += 1


def paired_orders(rounds: int, rng: random.Random) -> list[bool]:
    orders = [True] * (rounds // 2) + [False] * (rounds // 2)
    if rounds % 2:
        orders.append(rng.random() < 0.5)
    rng.shuffle(orders)
    return orders


def measure_cases(
    cases: list[BenchmarkCase],
    *,
    iterations: int,
    warmups: int,
    rng: random.Random,
) -> dict[tuple[str, str], CaseResult]:
    results = {(case.label, case.operation_name): CaseResult() for case in cases}
    warmup_order_by_case = {
        (case.label, case.operation_name): paired_orders(warmups, rng) for case in cases
    }
    measurement_order_by_case = {
        (case.label, case.operation_name): paired_orders(iterations, rng) for case in cases
    }

    for round_index in range(warmups):
        schedule = list(cases)
        rng.shuffle(schedule)
        for case in schedule:
            run_paired_round(
                case,
                direct_first=warmup_order_by_case[(case.label, case.operation_name)][round_index],
            )

    for round_index in range(iterations):
        schedule = list(cases)
        rng.shuffle(schedule)
        for case in schedule:
            run_paired_round(
                case,
                direct_first=measurement_order_by_case[(case.label, case.operation_name)][
                    round_index
                ],
                result=results[(case.label, case.operation_name)],
            )

    return results


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_proxy(binary: str, *, timing: bool, buffer_bytes: int | None) -> ProxyProcess:
    port = free_port()
    env = os.environ.copy()
    if timing:
        env["ROAR_PROXY_TIMING"] = "1"
    else:
        env.pop("ROAR_PROXY_TIMING", None)
    if buffer_bytes is not None:
        env["ROAR_PROXY_BUFFER_RESPONSE_BYTES"] = str(buffer_bytes)
    else:
        env.pop("ROAR_PROXY_BUFFER_RESPONSE_BYTES", None)

    proc = subprocess.Popen(
        [binary, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    handle = ProxyProcess(process=proc, port=port)

    def _reader(stream_name: str, sink: list[str]) -> None:
        stream = getattr(proc, stream_name)
        assert stream is not None
        for line in stream:
            sink.append(line.rstrip("\n"))

    handle.stdout_thread = threading.Thread(
        target=_reader, args=("stdout", handle.stdout_lines), daemon=True
    )
    handle.stderr_thread = threading.Thread(
        target=_reader, args=("stderr", handle.stderr_lines), daemon=True
    )
    handle.stdout_thread.start()
    handle.stderr_thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if any(line.startswith("ROAR_PROXY_READY") for line in handle.stdout_lines):
            return handle
        if proc.poll() is not None:
            raise RuntimeError(
                f"proxy exited early with code {proc.returncode}: {' | '.join(handle.stderr_lines)}"
            )
        time.sleep(0.05)

    proc.kill()
    proc.wait(timeout=3)
    raise RuntimeError("proxy did not become ready within 10 seconds")


def stop_proxy(handle: ProxyProcess) -> None:
    if handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            handle.process.wait(timeout=3)

    if handle.stdout_thread and handle.stdout_thread.is_alive():
        handle.stdout_thread.join(timeout=1)
    if handle.stderr_thread and handle.stderr_thread.is_alive():
        handle.stderr_thread.join(timeout=1)


def summarize_proxy_timing(lines: list[str]) -> dict[str, float]:
    samples: dict[str, list[float]] = {}
    for line in lines:
        match = TIMING_RE.match(line)
        if not match:
            continue
        samples.setdefault(match.group("label"), []).append(float(match.group("ms")))
    return {label: round(mean_ms(values), 3) for label, values in sorted(samples.items())}


def main() -> int:
    args = parse_args()
    bucket = args.bucket
    auto_created = False
    if not bucket:
        if not args.create_bucket:
            raise SystemExit("--bucket is required unless --create-bucket is set")
        bucket = f"roar-proxy-bench-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        auto_created = True

    assert bucket is not None
    prefix = args.prefix or f"bench/proxy-live/{int(time.time())}-{uuid.uuid4().hex[:8]}"
    binary = resolve_binary(args.binary)

    direct_client = boto3_client()
    ensure_bucket(direct_client, bucket, create_bucket=args.create_bucket)

    keys: dict[str, str] = {}
    for label, size_bytes in FILE_SIZES.items():
        key = f"{prefix}/{label}.bin"
        direct_client.put_object(Bucket=bucket, Key=key, Body=payload(size_bytes))
        keys[label] = key

    proxy = start_proxy(binary, timing=args.timing, buffer_bytes=args.buffer_bytes)
    proxy_client = boto3_client(endpoint_url=f"http://127.0.0.1:{proxy.port}")
    try:
        print(f"bucket={bucket}")
        print(f"prefix={prefix}")
        print(f"binary={binary}")
        print(f"proxy_endpoint=http://127.0.0.1:{proxy.port}")
        print(f"seed={args.seed}")
        print(f"buffer_bytes={'default' if args.buffer_bytes is None else args.buffer_bytes}")

        benchmark_cases: list[BenchmarkCase] = []
        for label, size_bytes in FILE_SIZES.items():
            key = keys[label]
            range_value = range_header(
                size_bytes,
                ratio=args.range_ratio,
                minimum=args.range_min_bytes,
                cap=args.range_cap_bytes,
            )

            benchmark_cases.extend(
                [
                    BenchmarkCase(
                        label=label,
                        operation_name="GET",
                        direct_operation=lambda key=key: read_body(
                            direct_client.get_object(Bucket=bucket, Key=key)
                        ),
                        proxy_operation=lambda key=key: read_body(
                            proxy_client.get_object(Bucket=bucket, Key=key)
                        ),
                    ),
                    BenchmarkCase(
                        label=label,
                        operation_name="RANGE",
                        direct_operation=lambda key=key, range_value=range_value: read_body(
                            direct_client.get_object(Bucket=bucket, Key=key, Range=range_value)
                        ),
                        proxy_operation=lambda key=key, range_value=range_value: read_body(
                            proxy_client.get_object(Bucket=bucket, Key=key, Range=range_value)
                        ),
                    ),
                ]
            )

        results = measure_cases(
            benchmark_cases,
            iterations=args.iterations,
            warmups=args.warmups,
            rng=random.Random(args.seed),
        )

        for label in FILE_SIZES:
            get_result = results[(label, "GET")]
            range_result = results[(label, "RANGE")]

            direct_get_mean = mean_ms(get_result.direct_samples)
            proxy_get_mean = mean_ms(get_result.proxy_samples)
            direct_range_mean = mean_ms(range_result.direct_samples)
            proxy_range_mean = mean_ms(range_result.proxy_samples)

            print(
                f"file={label:<6} GET: {direct_get_mean:.2f} -> {proxy_get_mean:.2f}ms "
                f"({percent_delta(proxy_get_mean, direct_get_mean):+.1f}%)  "
                f"RANGE: {direct_range_mean:.2f} -> {proxy_range_mean:.2f}ms "
                f"({percent_delta(proxy_range_mean, direct_range_mean):+.1f}%)"
            )
            print(
                " "
                f"paired_delta GET mean={mean_ms(get_result.paired_deltas):+.2f}ms "
                f"median={median_ms(get_result.paired_deltas):+.2f}ms "
                f"order={get_result.direct_first_count} direct-first / {get_result.proxy_first_count} proxy-first"
            )
            print(
                " "
                f"paired_delta RANGE mean={mean_ms(range_result.paired_deltas):+.2f}ms "
                f"median={median_ms(range_result.paired_deltas):+.2f}ms "
                f"order={range_result.direct_first_count} direct-first / {range_result.proxy_first_count} proxy-first"
            )
    finally:
        stop_proxy(proxy)
        timing_summary = summarize_proxy_timing(proxy.stderr_lines)
        if timing_summary:
            print("proxy_timing_summary=", timing_summary)
        maybe_cleanup_bucket(direct_client, bucket, args.keep_bucket, auto_created)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
