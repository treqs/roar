# Ray Worker Lineage: Design Options

**Status:** Draft for review  
**Author:** Rex  
**Date:** 2026-02-26

---

## Problem Statement

When a user runs `roar run python train.py` and `train.py` uses Ray, roar needs to construct
**total lineage**: a complete, reproducible record of every file and S3 object read or written
by the driver *and all workers* across all nodes — attributed to the specific task that
produced or consumed each artifact — and reconstruct it into the driver's single lineage graph.

This is hard because:

1. **Workers run on remote nodes** — no shared filesystem with the driver
2. **Worker processes are long-lived** — one worker process executes many tasks (Ray pool reuse)
3. **Content hashing is expensive** — hashing at write-time inside a hot worker adds latency
4. **eBPF requires capabilities** — remote nodes may not grant `CAP_BPF`
5. **Task attribution** — a file written in task T7 on node N2 must be traceable to T7, not
   just "some worker"

### Goal Anchor (from `~/dev/ROAR_GOALS.md`)

| # | Goal |
|---|------|
| 1 | Reproducibility default via content-based artifact tracking and replayable lineage |
| 2 | Low friction — observe existing commands, no pipeline DSL |
| 3 | Local-first lineage with optional GLaaS registration |
| 4 | Reliable tracing across backends (eBPF, preload, ptrace) |
| 5 | Performance and maintainability |

Total lineage in the Ray context means: **every artifact hash is derivable from the driver's
job record, without the user changing their Ray code**.

---

## Current State

What's working today (proven by e2e tests, not yet wired to `roar run`):

- `sitecustomize.py` patches `ray.init` → injects `runtime_env` with worker setup hook
- Workers start with `worker_process_setup_hook = "roar.ray.worker.setup"` which patches
  `builtins.open()` for file I/O capture
- Workers patch `boto3` clients to capture S3 ETags on put/get
- A `RoarLogCollectorActor` aggregates events from all workers via Ray object store
- Per-node `RoarNodeAgent` actors collect logs and handle autoscaling (new nodes mid-job)
- `ray.shutdown()` is patched to trigger collection and write to `.roar/roar.db`
- `LD_PRELOAD` wrapper (`libroar_tracer_preload.so`) is distributed to workers via
  `working_dir` and activated via `py_executable`

**Current gaps:**
- `roar run` doesn't set `ROAR_WRAP=1` (Ray patching never activates from CLI) — in flight
- Worker local files: path captured, **no content hash** (blake3/sha256)
- S3 objects: ETag captured (MD5-based), not blake3
- Composite job hash: not computed for Ray jobs
- `roar status` / `roar show` don't display per-task or per-worker breakdowns

---

## Option A: Extend the Current Architecture (Incremental)

**Approach:** Keep the `worker_process_setup_hook` model. Add content hashing inside
`worker.py`'s `_tracking_open` at file-close time. Compute composite hash in the collector.

### How it works

```
Driver: roar run python train.py
  │
  ├─ sitecustomize patches ray.init
  ├─ ROAR_WRAP=1 / ROAR_JOB_ID / ROAR_PROJECT_DIR set in subprocess env  ← Phase 1
  │
  └─ Worker processes (long-lived, many tasks each):
       worker_process_setup_hook patches open()
       LD_PRELOAD patches syscalls (optional, best-effort)
       boto3 patched for S3 ETag capture
       On each file close (write mode):
         hash file content → blake3                          ← NEW
         log(path, hash, task_id, node_id, ts)
       Events buffered → RoarLogCollectorActor
       
  On ray.shutdown():
    Collector reads actor events
    Merges per-task, per-node events
    Computes composite hash across all outputs              ← NEW
    Writes unified job record to .roar/roar.db
```

### Hashing strategy for local files

```python
# In worker.py _tracking_open, wrap the file object:
class _TrackedWriteFile:
    def __init__(self, real_file, path):
        self._f = real_file
        self._path = path
        self._hasher = blake3()

    def write(self, data):
        self._hasher.update(data if isinstance(data, bytes) else data.encode())
        return self._f.write(data)

    def close(self):
        self._f.close()
        digest = self._hasher.hexdigest()
        _log_access(self._path, "w", hash_value=digest, ...)
```

**Problem:** `blake3` may not be installed on remote nodes. Mitigation: ship `blake3` wheel
in `runtime_env.pip` alongside `roar-cli`, or fall back to `hashlib.sha256` (stdlib).

Alternative: hash at `close()` by reading the file back (`open(path, 'rb').read()`). Simpler
but doubles I/O for large files. Use streaming hash-during-write approach above instead.

### Pros

- Least architectural change — builds on what's proven
- Worker reuse handled naturally (per-task attribution already works)
- LD_PRELOAD covers syscall-level I/O in addition to Python-level
- No subprocess overhead per task
- GLaaS registration works unchanged — driver job record already has the right shape

### Cons

- Hashing overhead on every write inside workers (mitigable: opt-in via
  `ROAR_WORKER_HASH_FILES=1`, default off until benchmarked)
- S3 ETags are MD5, not blake3 — artifact identity across local and S3 uses different hash
  algorithms. Minor inconsistency but not a correctness problem.
- Still no eBPF on remote nodes (capability constraint) — preload only

### Goal alignment

| Goal | Rating | Notes |
|------|--------|-------|
| 1. Reproducibility | ✅ | Full lineage with content hashes once hashing added |
| 2. Low friction | ✅ | No user code changes |
| 3. Local-first | ✅ | All artifacts land in driver's `.roar/roar.db` |
| 4. Reliable tracing | ✅ | Preload on workers; eBPF on driver |
| 5. Performance | ⚠️  | Hash-on-write overhead; opt-in mitigates |

---

## Option B: `roar run` as `py_executable` (Worker-as-Job)

**Approach:** Set `py_executable = "roar-worker-run"` — a thin shim that wraps each worker
process with `roar run`-level tracing. Each worker process produces its own lineage fragment.
The driver collects fragments and merges them into the main job.

```
py_executable = "roar-worker-run"
  = bash -c "roar run --fragment-mode --job-id=$ROAR_JOB_ID python3 $@"
```

### Fragment mode

A new `--fragment-mode` flag for `roar run` that:
- Skips the `roar init` / `.roar` directory check
- Skips the git clean check
- Writes lineage to a temp file (e.g., `/tmp/roar-fragment-{worker_id}.json`) instead of DB
- Computes full blake3 content hashes for all outputs (existing `roar run` behavior)
- Signals the driver via the actor aggregator when done

The driver's collector then reads fragment files from the actor, merges them, and writes a
single unified job record to `.roar/roar.db`.

### The worker reuse problem

**This is the critical constraint.** Ray worker processes are reused across many task
invocations. `roar run` wraps a single subprocess — it traces start-to-exit. In Ray, one
worker process runs 50 tasks; `roar run` sees all 50 as one undifferentiated blob with no
task attribution.

**Workaround: force single-task-per-process workers.**

Ray supports this via `max_calls=1` on `@ray.remote`:

```python
@ray.remote(max_calls=1)
def my_task(shard):
    ...
```

With `max_calls=1`, each worker process handles exactly one task invocation and exits.
`roar run` maps cleanly: one process = one task = one lineage fragment.

**The catch:** `max_calls=1` forces a new worker process per task — cold start cost on every
invocation. For short tasks this is prohibitive. For long tasks (training runs, heavy data
processing) this is fine and is actually a common pattern.

### Shim design

```bash
#!/bin/bash
# roar-worker-run — installed as entry point by roar-cli
# Called by Ray as: roar-worker-run <python_args>
# We need: roar run python3 <python_args>

exec roar run \
  --fragment-mode \
  --job-id "${ROAR_JOB_ID:-unknown}" \
  --worker-id "$(python3 -c 'import ray; print(ray.get_runtime_context().get_worker_id())')" \
  python3 "$@"
```

### Pros

- Workers get **full** `roar run` tracing: LD_PRELOAD/eBPF, blake3 hashes, package capture,
  env reads — everything the driver gets
- Content hashes come for free (existing `roar run` behavior)
- Clean separation: each task is its own reproducible unit
- Fragment merge is content-addressed: same hash = same artifact, dedup is trivial

### Cons

- **Worker reuse**: only works correctly with `max_calls=1` — requires user to change their
  `@ray.remote` decorators, violating Goal 2 (low friction)
- **Cold start cost** per task with `max_calls=1`
- **Fragile shim**: worker ID lookup adds Ray API call at startup
- **Fragment collection**: new infrastructure needed (driver must wait for all fragments)
- Complex failure modes: if a worker crashes mid-task, partial fragment must be handled

### Goal alignment

| Goal | Rating | Notes |
|------|--------|-------|
| 1. Reproducibility | ✅ | Best possible — full roar run tracing per task |
| 2. Low friction | ❌ | Requires `max_calls=1` on user's `@ray.remote` |
| 3. Local-first | ✅ | Fragments merge into driver DB |
| 4. Reliable tracing | ✅ | Full backend stack per worker |
| 5. Performance | ❌ | Cold start per task; prohibitive for short tasks |

**Verdict:** Compelling for long-running single-task-per-process workloads, but violates the
low friction goal for general use. Best suited as an opt-in mode for power users.

---

## Option C: `roar-worker` Entry Point (Hybrid)

**Approach:** A new lightweight `roar-worker` command that sits between Option A (hook in
existing process) and Option B (full `roar run` subprocess). It runs as `py_executable` but
is designed specifically for the Ray worker lifecycle.

Key differences from Option B:
- **Handles multi-task workers** — tracks task boundaries via `get_runtime_context()`
- **No git check, no DB setup** — pure lineage fragment writer
- **Hash-on-close built in** — streaming blake3 per file write
- **Task-scoped records** — one lineage fragment per *task*, not per process
- **Native shim, not bash** — installed as a Python entry point

```
py_executable = "roar-worker"

roar-worker invocation:
  - starts (long-lived process, like a normal Ray worker)
  - patches open() + boto3 (like Option A)
  - tracks task context via ray.get_runtime_context() on each open() call
  - on each task boundary (detectable via context change): flush fragment for completed task
  - writes fragments to actor aggregator tagged with task_id + node_id + blake3 hashes
  - driver collector merges fragments: same hash = same artifact (content-addressed dedup)
```

### Task boundary detection

Ray doesn't expose a "task started/finished" callback directly, but task ID changes are
detectable:

```python
def _current_task_id():
    try:
        ctx = ray.get_runtime_context()
        return ctx.get_task_id()
    except Exception:
        return None

# In _tracking_open:
task_id = _current_task_id()
if task_id != _last_task_id:
    _flush_fragment(_last_task_id)   # flush completed task
    _last_task_id = task_id
    _current_task_accesses.clear()
```

### Fragment schema

```json
{
  "job_id": "abc123",
  "task_id": "task-0001",
  "node_id": "node-0002",
  "worker_id": "worker-0003",
  "reads":  [{"path": "s3://bucket/data.parquet", "hash": "etag:abc", "ts": 1234}],
  "writes": [{"path": "/tmp/checkpoint.pt", "hash": "blake3:xyz", "ts": 1235}],
  "packages": {"torch": "2.1.0"},   // optional, first task only
  "duration_ms": 4200
}
```

### Driver merge

The collector assembles fragments into the full lineage graph:
- Group by `job_id`
- Dedup artifacts by hash (same hash → same artifact row)
- Build lineage edges: task T reads artifact A, writes artifact B
- Compute composite job hash: blake3(sorted(output_hashes))
- Write single unified job record to `.roar/roar.db`

### Pros

- **No `max_calls=1` required** — works with long-lived worker processes
- **Per-task attribution** — each task is a traceable unit
- **Content hashes for all artifacts** — blake3 for local files, ETag for S3
- **No user code changes** — injected transparently via `py_executable`
- **Clean fragment schema** — designed for merge from the start
- Composite hash enables GLaaS dedup across runs

### Cons

- **New entry point to build and maintain** — more surface area than Option A
- **Task boundary detection is heuristic** — relies on task ID changing, which may have
  edge cases (e.g., actor tasks, nested remote calls)
- **Fragment collection latency** — driver must wait for all fragments before writing DB
- **`roar-worker` must be installed on remote nodes** — via `runtime_env.pip = ["roar-cli"]`
  (already the case, since `roar-cli` includes `roar-worker` as an entry point)

### Goal alignment

| Goal | Rating | Notes |
|------|--------|-------|
| 1. Reproducibility | ✅ | Per-task lineage with content hashes |
| 2. Low friction | ✅ | No user code changes |
| 3. Local-first | ✅ | Fragments merge into driver DB |
| 4. Reliable tracing | ✅ | Preload in worker, eBPF on driver |
| 5. Performance | ⚠️  | Hash-on-write overhead (same as A); no cold start cost |

---

## Comparison Matrix

| Criterion | Option A (Extend) | Option B (roar run) | Option C (roar-worker) |
|-----------|:-----------------:|:-------------------:|:----------------------:|
| No user code changes | ✅ | ❌ (`max_calls=1`) | ✅ |
| Per-task attribution | ✅ | ✅ | ✅ |
| Content hash (local files) | ⚠️ (add) | ✅ (free) | ✅ (built-in) |
| Content hash (S3) | ⚠️ (ETag/MD5) | ✅ (blake3 via proxy) | ⚠️ (ETag/MD5) |
| Composite job hash | ⚠️ (add) | ✅ (free) | ✅ (built-in) |
| Works with worker reuse | ✅ | ❌ | ✅ |
| Short task overhead | ✅ | ❌ | ✅ |
| Long task fidelity | ⚠️ | ✅ | ✅ |
| Implementation risk | Low | Medium | Medium |
| New infrastructure | Minimal | Fragment collect | Fragment collect + entry point |
| GLaaS registration | ✅ | ✅ | ✅ |

---

## Recommendation

**Phase 1 (in flight):** Complete Option A activation — wire `ROAR_WRAP=1` into `roar run`,
fix job ID lookup in collector, guard against double-counting. This gets real end-to-end
tracing working from the CLI.

**Phase 2: Option A + content hashing.** Add streaming blake3 at file-close in `worker.py`,
opt-in via `ROAR_WORKER_HASH_FILES=1` initially. Add composite hash in collector. This closes
the reproducibility gap (Goal 1) without architectural risk.

**Phase 3: Option C (roar-worker) as the long-term target.** Option C gives the cleanest
design — purpose-built for distributed task lineage, no user code changes, content-addressed
fragments that reconstruct naturally. Build it once Option A+hashing is stable and we have
real workloads to validate against.

**Option B as an opt-in power feature.** `roar run --fragment-mode` or
`@ray.remote(max_calls=1)` + `py_executable = "roar-worker-run"` for users who want
absolute maximum tracing fidelity on long single-task-per-process workloads. Document it but
don't make it the default.

### Suggested roadmap

```
Now        Phase 1: roar run activates Ray tracing (Codex in flight)
           
Near-term  Phase 2: content hashing in workers
           - streaming blake3 in _tracking_open (Option A extension)
           - composite hash in collector
           - S3: promote ETag to blake3 via proxy (if proxy is in the loop)
           - opt-in: ROAR_WORKER_HASH_FILES=1

Medium     Phase 3: roar-worker entry point (Option C)
           - fragment schema + driver merge
           - task boundary detection
           - roar-worker installed via runtime_env.pip as part of roar-cli
           - roar-worker replaces worker_process_setup_hook + py_executable wrapper
           
Later      Option B opt-in
           - --fragment-mode flag on roar run
           - document max_calls=1 pattern
           - good for: hyperparameter search, per-trial provenance
```

---

## Open Questions for Trevor

1. **S3 hash consistency:** ETags are MD5. Should S3 artifacts use blake3 (computed via
   proxy) so all artifacts use the same algorithm? Or is ETag sufficient for identity?

2. **`ROAR_WORKER_HASH_FILES` default:** On or off? Hashing adds latency per file write
   inside workers. We don't have benchmark numbers yet for real workloads.

3. **Option C timing:** Should `roar-worker` be a Phase 2 item (build it soon, while the
   architecture is fresh) or Phase 3 (wait until we have real usage data)? The fragment
   schema is the foundational piece — easier to design now than retrofit later.

4. **Actor task attribution:** Ray actors (long-lived stateful `@ray.remote` classes) behave
   differently from tasks — they don't have a "task ID" in the same sense. Should actor
   method calls be attributed differently? Or treated as a single long-lived task per actor?

5. **GLaaS shape for distributed jobs:** Does GLaaS need to understand the concept of
   "sub-tasks" under a parent job, or is a flat list of artifacts under one job_id sufficient
   for the registration and search use cases?
