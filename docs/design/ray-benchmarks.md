# Ray Integration Benchmarks

## Goal

Quantify the overhead roar adds to Ray jobs across each instrumentation layer.
Results guide tuning decisions and serve as regression guards in CI.

Follow the pattern established in `tests/benchmarks/bench_tracer_overhead.py`:
linear regression decomposition, warmup runs, JSON result storage.

---

## What to Measure

| # | Benchmark | Question |
|---|-----------|----------|
| 1 | **Job startup** | How much does `working_dir` upload + `py_executable` wrapper add to `ray.init()` / first-task latency? |
| 2 | **Actor IPC throughput** | How many log events/sec can `RoarLogCollectorActor` absorb from N concurrent workers before becoming a bottleneck? |
| 3 | **Preload per-call overhead** | What is the added latency per `open()` call inside a Ray worker with `LD_PRELOAD` set? |
| 4 | **Proxy S3 overhead** | What latency does `roar-proxy` add per S3 PutObject / GetObject against a local MinIO? |
| 5 | **End-to-end job overhead** | Whole-job wall-clock overhead: roar-instrumented vs uninstrumented, across varying task counts and I/O rates. |

---

## Benchmark 1: Job Startup Overhead

**File:** `tests/benchmarks/bench_ray_startup.py`

**What:** Time from `ray.init()` call to first remote task completing, comparing:
- Baseline: `ray.init()` with no runtime_env
- With roar: `ray.init()` with roar's `working_dir` + `py_executable`

**Variables:**
- `working_dir` size (small: ~1 MB preload .so only; large: with additional user files)
- Cold start (new Ray session) vs warm (Ray already initialized, env cached)

**Method:**
```python
# Baseline
start = time.perf_counter()
ray.init()
ray.get(noop.remote())
baseline = time.perf_counter() - start
ray.shutdown()

# With roar
start = time.perf_counter()
ray.init(runtime_env=roar_runtime_env)
ray.get(noop.remote())
with_roar = time.perf_counter() - start
ray.shutdown()
```

**Output:**
```
--- Job Startup Overhead ---
  Baseline (no runtime_env):  2.3s ± 0.1s
  With roar (cold):           5.1s ± 0.2s  (+2.8s, +121%)
  With roar (warm, cached):   2.5s ± 0.1s  (+0.2s, +9%)
```

**Target:** Warm overhead <1s. Cold overhead acceptable up to ~10s (first-run cost only).

---

## Benchmark 2: Actor IPC Throughput

**File:** `tests/benchmarks/bench_ray_actor_ipc.py`

**What:** Maximum event throughput of `RoarLogCollectorActor` under concurrent load.
Decomposes into: actor `append_batch` latency vs. worker concurrency scaling.

**Variables:**
- Batch size: 1, 10, 50, 100 events per `append_batch.remote()` call
- Worker concurrency: 1, 2, 4, 8, 16, 32 concurrent workers
- Event payload size: minimal vs. realistic (full path + task_id + metadata)

**Method:**
```python
# For each (batch_size, n_workers):
# Spawn n_workers tasks, each calling append_batch.remote() for 10 seconds.
# Count total events delivered. Throughput = events / 10s.
@ray.remote
def hammer_actor(actor, batch_size, duration_s):
    batch = [{"path": "/tmp/foo", "mode": "r", "task_id": "abc", "ts": 1.0}] * batch_size
    start = time.time()
    count = 0
    while time.time() - start < duration_s:
        ray.get(actor.append_batch.remote(batch))
        count += batch_size
    return count
```

**Output:**
```
--- Actor IPC Throughput ---
Batch size  Workers  Events/sec   Latency/batch
       1       1       4,200          0.24ms
      10       1      38,000          0.26ms
      50       4     180,000          1.10ms  (bottleneck approaching)
     100      16     250,000          6.40ms  (actor saturated)

Saturation point: ~16 workers with batch_size=50 → ~240k events/sec.
Recommendation: batch_threshold=50, flush_interval=0.5s for <16 workers.
```

**Target:** >100k events/sec at 8 concurrent workers without queue buildup.

---

## Benchmark 3: Preload Per-Call Overhead (Ray Worker Context)

**File:** `tests/benchmarks/bench_ray_preload.py`

**What:** `open()` call latency inside a Ray task, with and without `LD_PRELOAD`.

This extends `bench_tracer_overhead.py`'s approach into the Ray worker context —
the preload overhead may differ from a standalone process because of Ray's worker
environment (fork model, gc, etc.).

**Variables:**
- File count: 0, 100, 500, 1000, 5000 `open()` calls per task
- File size: 0 bytes (stat only), 256 B, 4 KB, 1 MB

**Method:**
```python
@ray.remote
def file_io_task(n_files, file_size):
    """Write n_files files of file_size bytes each, then read them."""
    with tempfile.TemporaryDirectory() as d:
        for i in range(n_files):
            p = os.path.join(d, f"f{i}")
            with open(p, "wb") as f:
                f.write(b"x" * file_size)
        for i in range(n_files):
            p = os.path.join(d, f"f{i}")
            with open(p, "rb") as f:
                f.read()

# Run with ROAR_WRAP=0 (baseline) and ROAR_WRAP=1 (with preload)
```

**Regression:** Same OLS decomposition as `bench_tracer_overhead.py`:
- Startup overhead (intercept): fixed cost per task regardless of I/O
- Per-file overhead (slope): marginal cost per `open()` call

**Output:**
```
--- Preload Overhead in Ray Workers ---
  Startup overhead:    3.2ms  (Ray worker init + preload init)
  Per-file overhead:   0.8μs  (per open() call)

  Projected overhead:
    100 files:   3.3ms
    1000 files: 4.0ms
    5000 files: 7.2ms
```

**Target:** Per-call overhead <2μs (matches standalone process performance).

---

## Benchmark 4: Proxy S3 Overhead

**File:** `tests/benchmarks/bench_ray_proxy.py`

**What:** Latency added by `roar-proxy` to S3 operations from within a Ray task.
Compares direct MinIO → roar-proxy → MinIO.

Uses the Docker MinIO instance from the e2e test harness.

**Variables:**
- Operation: PutObject, GetObject
- Object size: 1 KB, 1 MB, 100 MB
- Concurrency: 1, 4, 8 parallel S3 operations

**Method:**
```python
@ray.remote
def s3_task(bucket, key, size_bytes, use_proxy):
    import boto3
    client = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL"))
    data = b"x" * size_bytes
    start = time.perf_counter()
    client.put_object(Bucket=bucket, Key=key, Body=data)
    client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return time.perf_counter() - start
```

**Output:**
```
--- Proxy S3 Overhead ---
  Operation   Size     Direct    Via proxy  Overhead   Overhead%
  PutObject   1KB      1.2ms     1.5ms      +0.3ms     +25%
  PutObject   1MB      3.4ms     3.8ms      +0.4ms     +12%
  PutObject   100MB    210ms     215ms      +5ms       +2%
  GetObject   1KB      1.1ms     1.4ms      +0.3ms     +27%
  GetObject   1MB      4.1ms     4.4ms      +0.3ms     +7%
```

**Target:** Fixed overhead <1ms regardless of object size. Percentage overhead
decreases with larger objects (amortized).

---

## Benchmark 5: End-to-End Job Overhead

**File:** `tests/benchmarks/bench_ray_e2e.py`

**What:** Wall-clock overhead for a complete Ray job, roar-instrumented vs baseline.
The reference workload is a realistic ML-pipeline-ish job: read data, transform,
write results, with some S3 operations.

**Variables:**
- Task count: 1, 4, 16, 64 parallel tasks
- I/O per task: low (10 files, 0 S3), medium (100 files, 5 S3), high (1000 files, 20 S3)
- Worker nodes: 1 (single-node), 2 (multi-node via Docker)

**Reference workload per task:**
```python
@ray.remote
def reference_task(n_files, n_s3, tmpdir):
    # File I/O
    for i in range(n_files):
        with open(f"{tmpdir}/f{i}.dat", "wb") as f:
            f.write(os.urandom(4096))
    # S3 I/O (against MinIO)
    for i in range(n_s3):
        s3.put_object(Bucket="bench", Key=f"task-{task_id}/{i}", Body=b"x" * 1024)
    return n_files + n_s3
```

**Output (example):**
```
--- End-to-End Job Overhead (single node) ---
                     Baseline    With roar   Overhead    %
tasks=1,   I/O=low    0.8s        1.1s       +0.3s     +38%
tasks=4,   I/O=low    1.2s        1.5s       +0.3s     +25%
tasks=16,  I/O=low    2.1s        2.4s       +0.3s     +14%
tasks=1,   I/O=med    2.4s        2.7s       +0.3s     +13%
tasks=4,   I/O=med    3.1s        3.5s       +0.4s     +13%
tasks=16,  I/O=med    4.8s        5.3s       +0.5s     +10%
tasks=1,   I/O=high  15.2s       15.9s       +0.7s     +5%
tasks=16,  I/O=high  21.4s       22.2s       +0.8s     +4%

Overhead amortizes with more I/O (startup cost dominates at low I/O).
```

**Target:** <10% overhead for I/O-medium workloads at any task count.

---

## Result Storage

Follow the existing pattern in `tests/benchmarks/results/`:

```
tests/benchmarks/results/
    ray_startup_latest.json
    ray_actor_ipc_latest.json
    ray_preload_latest.json
    ray_proxy_latest.json
    ray_e2e_latest.json
```

Each result file: JSON with `timestamp`, `git_commit`, `ray_version`, `results` array.

---

## Implementation Plan for Codex

1. `tests/benchmarks/bench_ray_startup.py`
2. `tests/benchmarks/bench_ray_actor_ipc.py`
3. `tests/benchmarks/bench_ray_preload.py`
4. `tests/benchmarks/bench_ray_proxy.py`
5. `tests/benchmarks/bench_ray_e2e.py`
6. `tests/benchmarks/run_ray_benchmarks.sh` — runner that executes all five,
   saves results to `results/`, prints summary table

Each script must:
- Accept `--iterations N` and `--quick` flags (`--quick` reduces scale for CI)
- Save results as JSON to `tests/benchmarks/results/`
- Print a human-readable summary table on stdout
- Require the Docker Ray cluster to be up (check and error clearly if not)
- Exit 0 on success, non-zero if any benchmark errors (not just if overhead is high)

Use the Docker compose cluster from `tests/e2e/ray/docker-compose.yml`.

---

## Open Questions

1. **CI integration:** Should benchmarks run in CI on every PR (slow, ~10 min)
   or only on release branches? Recommend: `--quick` mode in CI (reduced
   iterations, fewer scale points), full mode manually before releases.

2. **Regression threshold:** At what overhead % should CI fail? Recommend:
   >20% regression vs previous run triggers a warning; >50% triggers failure.
   Store baseline in `tests/benchmarks/results/ray_e2e_baseline.json`.
