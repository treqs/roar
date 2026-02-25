# Ray Integration Design Plan

## Goal

When a user runs `roar run python train.py` and `train.py` uses Ray, roar
automatically instruments all Ray workers (local and remote) with:

- Native file I/O tracers (eBPF / preload / ptrace)
- S3 proxy for object storage lineage
- Per-task attribution (which `@ray.remote` task read/wrote which files)

**No changes to user code.** The user doesn't import roar, add decorators, or
configure anything Ray-specific.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────┐
│  roar run python train.py                          │
│                                                     │
│  TracerService wraps the driver process             │
│  ProxyService starts local S3 proxy                 │
│  sitecustomize.py patches:                          │
│    ├─ builtins.open()    (file I/O capture)         │
│    ├─ builtins.__import__                           │
│    ├─ ray.init()         (inject roar into workers) │
│    └─ ray.remote()       (per-task attribution)     │
│                                                     │
│  When ray.init() is called:                         │
│    1. Install roar on remote nodes via runtime_env  │
│    2. Start per-node roar agent (tracer + proxy)    │
│    3. Propagate env vars to all workers             │
│    4. Register collection callback for shutdown     │
└────────────────────────────────────────────────────┘
         │                         │
    Local workers              Remote nodes
    (same machine)             (Ray cluster)
         │                         │
    ┌────┴─────┐             ┌─────┴──────┐
    │ eBPF     │             │ roar-agent  │
    │ already  │             │ (per node)  │
    │ sees all │             │  ├─ tracer  │
    │ worker   │             │  ├─ proxy   │
    │ I/O      │             │  └─ log     │
    └──────────┘             │    collector│
                             └────────────┘
```

---

## Components

### 1. Ray Detection & Init Patching (`sitecustomize.py`)

**Mechanism:** Lazy-patch `ray.init` at import time.

When `import ray` is detected by our `__import__` hook:
- Monkey-patch `ray.init` with a wrapper that:
  1. Merges roar config into `runtime_env`
  2. Calls the real `ray.init`

What gets injected into `runtime_env`:

```python
runtime_env = {
    "pip": ["roar-cli"],                     # Install roar on remote nodes
    "env_vars": {
        "ROAR_SESSION_ID": "<session_id>",   # Attribution
        "ROAR_JOB_ID": "<job_id>",           # Attribution
        "ROAR_DRIVER_HOST": "<driver_ip>",   # For log collection
        "ROAR_DRIVER_PORT": "<port>",        # For log collection
        "ROAR_RAY_WORKER": "1",              # Flag: I'm a ray worker
    },
    "worker_process_setup_hook": "roar.ray.worker_setup:setup",
}
```

**Open question:** `runtime_env.pip` installs from PyPI. If roar isn't
published yet (or we need the exact version), we may need to use
`runtime_env.py_modules` + ship the wheel, or use a
`runtime_env.working_dir` approach. Alternatively, a conda channel or
a pre-built container image. **Decision needed.**

### 2. Per-Task Attribution (`ray.remote` patching)

**Mechanism:** Wrap `ray.remote` so each task execution is tagged.

When `ray` is imported, also patch `ray.remote`:

```python
_real_remote = ray.remote

def patched_remote(*args, **kwargs):
    cls_or_fn = _real_remote(*args, **kwargs)

    # Wrap the actual function/class so that when it executes,
    # it sets ROAR_TASK_ID in the worker's thread-local context
    original_execute = cls_or_fn._remote  # or however Ray invokes it

    def wrapped_remote(*a, **kw):
        task_id = generate_task_id(cls_or_fn, a, kw)
        # Tag all I/O from this point with task_id
        _thread_local.roar_task_id = task_id
        try:
            return original_execute(*a, **kw)
        finally:
            _thread_local.roar_task_id = None

    cls_or_fn._remote = wrapped_remote
    return cls_or_fn
```

The `tracking_open` in sitecustomize checks `_thread_local.roar_task_id`
and includes it in the log entry — giving us per-task file I/O attribution.

**Alternative approach:** Instead of patching `ray.remote`, use Ray's
built-in `runtime_context` inside the worker setup hook:

```python
ctx = ray.get_runtime_context()
task_id = ctx.get_task_id()
```

This is cleaner and doesn't require patching `ray.remote` at all — each
worker already knows its task ID at execution time.

### 3. Per-Node Roar Agent

A lightweight process that runs once per Ray node. Responsible for:

- **Starting the native tracer** (eBPF preferred, preload fallback) to
  capture syscall-level file I/O from all worker processes on that node
- **Starting the S3 proxy** on a known port, configured via
  `AWS_ENDPOINT_URL` env var propagated to workers
- **Collecting logs** from worker sitecustomize outputs
- **Shipping logs** back to the driver node on job completion

**Lifecycle:**

```
Worker setup hook runs (first worker on a node)
  → Checks: is roar-agent already running on this node?
  → If not: starts roar-agent as a background daemon
  → roar-agent starts tracer + proxy
  → roar-agent listens for shutdown signal

Driver job finishes
  → Driver sends "collect" signal to all node agents
  → Agents ship logs back via Ray object store or HTTP
  → Driver merges all logs into unified lineage
  → Agents shut down
```

**Implementation:** The agent could be:

- **(a) A Ray actor** — `@ray.remote(num_cpus=0, resources={"node:X": 0.001})`
  placed on each node. Pro: lifecycle managed by Ray. Con: uses Ray resources.
- **(b) A background daemon** — started by the first worker_setup_hook call
  on each node. Pro: no Ray resource cost. Con: cleanup is harder.
- **(c) A detached Ray actor** — survives task completion, explicitly killed
  by driver. Best of both worlds.

**Recommendation:** Option (c) — detached actor per node. Ray manages
placement and cleanup, minimal resource overhead.

### 4. Log Collection & Merging

**From each node we collect:**

1. **Tracer log** (msgpack) — native syscall-level file I/O
2. **Proxy log** — S3/GCS operations with ETags and byte ranges
3. **Worker sitecustomize logs** (JSON) — Python-level open() calls,
   imports, packages, with per-task attribution

**Collection mechanism:**

Option A: Ray object store — each agent puts its logs into the object store,
driver `ray.get()`s them. Simple, works cross-node, but limited by object
store size.

Option B: HTTP transfer — agent serves logs on a port, driver fetches.
More complex, but handles large logs.

Option C: Shared filesystem — if all nodes mount the same NFS/S3, write
logs to a known path. Simplest if available.

**Recommendation:** Option A for v1 (Ray object store). Logs are typically
<100MB. Fall back to Option C if a shared filesystem is detected.

**Merging:** The driver's `ExecutionJobRecorder` already merges tracer +
inject + proxy logs. We extend it to accept multiple log sets (one per node)
and merge them, deduplicating by `(path, operation)` and preserving
per-task attribution.

### 5. Binary Distribution

Roar's native binaries (tracer, proxy) must be on each Ray node.

**Options:**

| Approach | Pro | Con |
|----------|-----|-----|
| `pip install roar-cli` via runtime_env | Automatic, uses existing infra | Needs roar published to PyPI with platform wheels |
| Pre-built container image | Fast, reliable | Requires Docker/container runtime on Ray cluster |
| `runtime_env.working_dir` with binaries | Works without PyPI | Large upload per job, slow |
| Cluster setup script (ansible/terraform) | One-time cost | Manual ops step (violates "no ops" requirement) |
| Conda package | Cross-platform binary support | Needs conda on nodes |

**Recommendation:** `pip install roar-cli` via `runtime_env.pip` is the
cleanest path. Roar already builds platform wheels via maturin. This means
we need roar published to a package index (PyPI or private). For private
clusters, users could configure a private index URL.

---

## Implementation Phases

### Phase 1: Single-Node Ray (simplest, highest value)

**Scope:** `ray.init()` on the same machine. All workers are local.

**What we build:**
- [ ] Ray detection in sitecustomize (`__import__` hook for `ray`)
- [ ] `ray.init()` wrapper that injects env vars into `runtime_env`
- [ ] Worker setup hook that initializes per-worker log files
- [ ] Per-task attribution via `ray.get_runtime_context().get_task_id()`
- [ ] Extended `tracking_open()` that tags I/O with task ID
- [ ] Extended job recorder that merges multi-worker logs
- [ ] Log format v2: add `task_id`, `worker_pid`, `node_id` fields

**What we DON'T need yet:**
- Binary distribution (tracer already running on same machine)
- S3 proxy per node (driver proxy already covers local workers)
- Log shipping (all logs are local)

**Why this is enough for v1:** eBPF tracer already sees all processes on
the machine. The proxy's `AWS_ENDPOINT_URL` propagates via env vars.
Sitecustomize propagates via PYTHONPATH. We just need attribution.

### Phase 2: Multi-Node Ray

**Scope:** Ray cluster with remote nodes.

**What we build:**
- [ ] `roar.ray.agent` module — per-node agent (detached Ray actor)
- [ ] Agent binary: discovers + starts tracer and proxy on the node
- [ ] `runtime_env.pip` injection to install roar on remote nodes
- [ ] Log collection via Ray object store
- [ ] Extended merger for multi-node log sets
- [ ] Node discovery (detect which nodes are in the cluster)
- [ ] Graceful cleanup: shut down agents when driver exits

### Phase 3: Hardening

- [ ] Handle Ray autoscaling (new nodes joining mid-job)
- [ ] Handle worker failures and partial log collection
- [ ] Support Ray Data (`ray.data.read_*`) — verify native tracer captures
      Arrow I/O at syscall level; add Ray Data-specific attribution if needed
- [ ] Support Ray Serve (long-running workers)
- [ ] Performance benchmarks: measure overhead of instrumentation
- [ ] Configuration: `[ray]` section in `.roar/config.toml` for
      opting out, setting proxy port ranges, tracer mode, etc.

---

## File Layout

```
roar/
├── ray/                          # New module
│   ├── __init__.py
│   ├── init_patch.py             # ray.init() wrapper
│   ├── remote_patch.py           # ray.remote() wrapper (task attribution)
│   ├── worker_setup.py           # worker_process_setup_hook entry point
│   ├── agent.py                  # Per-node agent (detached actor)
│   ├── log_collector.py          # Collect + merge logs from nodes
│   └── env.py                    # Env var constants and helpers
├── services/
│   └── execution/
│       ├── inject/
│       │   └── sitecustomize.py  # Extended: ray import detection
│       └── job_recording.py      # Extended: multi-worker log merging
```

---

## Key Design Decisions Needed

1. **Package distribution:** How is roar installed on remote Ray nodes?
   PyPI wheel via `runtime_env.pip` is cleanest but requires publishing.

2. **Agent lifecycle:** Detached Ray actor vs background daemon? Actor is
   cleaner but uses Ray scheduler. Daemon is invisible but harder to clean up.

3. **Log format:** Extend existing msgpack/JSON formats with attribution
   fields, or create a new unified format?

4. **Proxy topology:** One proxy per node (workers share) vs one proxy per
   worker? Per-node is simpler and lower overhead.

5. **eBPF on remote nodes:** Requires `CAP_BPF` + `CAP_SYS_PTRACE` etc.
   If not available, fall back to preload. Should the agent auto-detect
   and choose?

6. **Testing:** How do we test multi-node Ray in CI? Use Ray's local
   cluster mode (`ray.init(num_cpus=N)`) for unit tests. Real cluster
   tests in a separate CI job with Docker Compose?

---

## Risks

| Risk | Mitigation |
|------|------------|
| `runtime_env.pip` is slow (installs on every job) | Use `runtime_env` caching; pre-build images for production |
| eBPF not available on remote nodes | Auto-fallback to preload in agent |
| Ray internal I/O pollutes lineage | Filter by Ray internal paths (raylet, object store, plasma) |
| Worker reuse across tasks pollutes attribution | Reset task context on each invocation via runtime_context |
| Large clusters = many logs | Aggregate per-node before shipping to driver |
| `ray.remote` patching breaks with Ray version updates | Pin to Ray API, use `ray.get_runtime_context()` for attribution instead of deep patching |
