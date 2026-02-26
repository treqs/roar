# Ray Native Tracing: Per-Node Agent Design

## Goal

Every Ray worker — on every node — should be instrumented with roar's native
tracers and proxy, exactly as `roar run` instruments a local process. No C
extension I/O escapes. No S3 call goes untracked.

---

## Current State & Gap

Today, workers are instrumented at the Python level only:
- `builtins.open()` patching (misses C extensions calling libc directly)
- `boto3` / `pandas` / `pyarrow` monkey-patching (brittle, format-specific)
- `AWS_ENDPOINT_URL` set to the **driver's** proxy (breaks on remote nodes)

What we're missing:
- **`libroar_tracer_preload.so`** intercepting libc `open()` / `read()` / `write()` on each worker
- **`roar-proxy`** running on each node (not just the driver) so workers can route S3 calls locally
- **Tracer + proxy log collection** from each node back to the driver

---

## Architecture

```
Driver node (_roar_ray_init)
────────────────────────────────────────────────────────────
  1. Generate wrapper script template (discovers preload lib at runtime)
  2. Inject runtime_env.py_executable = wrapper_path
     runtime_env.env_vars = { ROAR_PROXY_PORT, ROAR_JOB_ID, ... }
  3. Call real ray.init() → cluster starts, workers use wrapper as Python
  4. Spawn RoarNodeAgent on each node (non-blocking)

Worker process (started via wrapper script)
────────────────────────────────────────────────────────────
  wrapper.sh runs first:
    ├─ discovers local libroar_tracer_preload.so
    ├─ export LD_PRELOAD=/local/path/to/preload.so   ← active from birth
    ├─ export AWS_ENDPOINT_URL=http://127.0.0.1:PORT ← local proxy
    └─ exec python3 [worker args]

  Python worker starts:
    ├─ preload.so already intercepting all open()/read()/write()
    ├─ worker_process_setup_hook → Python-level patches (boto3, pandas, etc.)
    └─ logs → ROAR_TRACER_LOG_DIR on this node

RoarNodeAgent (per node, detached actor)
────────────────────────────────────────────────────────────
  ├─ starts roar-proxy on a free port
  ├─ writes wrapper script with local port baked in
  └─ on collect_logs(): reads tracer msgpack + proxy logs → returns to driver

Driver atexit (ray.shutdown patch)
────────────────────────────────────────────────────────────
  ├─ for each RoarNodeAgent: ray.get(agent.collect_logs.remote())
  ├─ merge tracer + proxy + Python-level logs into roar.db
  └─ ray.kill(agents)
```

---

## Components

### 1. `RoarNodeAgent` (`roar/ray/node_agent.py`)

A detached Ray actor placed on a specific node. Owns the tracer and proxy
lifecycle for that node.

```python
@ray.remote(num_cpus=0)
class RoarNodeAgent:
    def __init__(self, job_id: str, log_dir: str) -> None:
        self._job_id = job_id
        self._log_dir = log_dir
        self._proxy_process: subprocess.Popen | None = None
        self._proxy_port: int | None = None
        self._preload_lib_path: str | None = None
        self._tracer_log_dir: str = os.path.join(log_dir, "tracer")
        self._ready = False
        self._start()

    def _start(self) -> None:
        os.makedirs(self._tracer_log_dir, exist_ok=True)
        self._preload_lib_path = _discover_preload_lib()
        self._proxy_port = _start_proxy(self._job_id, self._log_dir)
        self._ready = True

    def ready(self) -> bool:
        return self._ready

    def get_proxy_port(self) -> int | None:
        return self._proxy_port

    def get_preload_lib_path(self) -> str | None:
        return self._preload_lib_path

    def get_tracer_log_dir(self) -> str:
        return self._tracer_log_dir

    def collect_logs(self) -> dict:
        """Returns {"proxy": [log_lines], "tracer": [msgpack_bytes_per_file]}"""
        return {
            "proxy": _read_proxy_logs(self._log_dir, self._job_id),
            "tracer": _read_tracer_logs(self._tracer_log_dir),
        }

    def shutdown(self) -> None:
        if self._proxy_process:
            self._proxy_process.terminate()
```

**Placement:** Use `options(resources={"node:<node_id>": 0.0001})` to pin each
actor to its node. Ray's `ray.nodes()` provides node IDs after `ray.init()`.

**One agent per node per job:** Named `roar-node-agent-<job_id>-<node_id>` in
namespace `"roar"`. Worker setup hook uses `ray.get_actor()` to find it.

---

### 2. Proxy per Node

Each `RoarNodeAgent.__init__` starts a `roar-proxy` subprocess on a random
available port. Uses existing `ProxyService` logic adapted for subprocess
management without the full `RunCoordinator` context:

```python
def _start_proxy(job_id: str, log_dir: str) -> int:
    from roar.services.execution.tracer_backends import find_proxy_binary
    binary = find_proxy_binary(package_path)
    port = _find_free_port()
    log_file = os.path.join(log_dir, f"proxy-{job_id}.log")
    process = subprocess.Popen(
        [binary, "--port", str(port), "--log", log_file],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_port(port, timeout=10)
    return port
```

---

### 3. Preload Library Injection via `py_executable` + `working_dir`

`runtime_env.py_executable` is a flat string (same for all nodes) pointing to
the executable Ray uses to start workers. Combined with `working_dir`, we can
distribute the preload library and wrapper script to every node and reference
them with a relative path — no driver-side path guessing required.

**How it works:**

Ray downloads `working_dir` to each node and starts workers with their cwd set
to that directory. So if we include `libroar_tracer_preload.so` and a wrapper
script in `working_dir`, the wrapper can reference the library as
`./libroar_tracer_preload.so` — valid on every node.

**Before `ray.init()`** (in `_roar_ray_init`):

```python
def _prepare_worker_runtime_env(runtime_env: dict, job_id: str) -> dict:
    import tempfile, shutil
    from roar.services.execution.tracer_backends import find_preload_library
    from pathlib import Path
    import roar

    tmp_dir = tempfile.mkdtemp(prefix="roar-worker-env-")

    # Copy preload library into temp dir (will be distributed via working_dir)
    preload_lib = find_preload_library(Path(roar.__file__).parent)
    if preload_lib:
        shutil.copy2(preload_lib, os.path.join(tmp_dir, "libroar_tracer_preload.so"))

    # Write wrapper script
    wrapper = os.path.join(tmp_dir, "roar_worker_wrapper.sh")
    with open(wrapper, "w") as f:
        f.write(textwrap.dedent(f"""
            #!/bin/bash
            # roar worker wrapper — sets LD_PRELOAD and proxy before starting Python.
            if [ -f "./libroar_tracer_preload.so" ]; then
                export LD_PRELOAD="$(pwd)/libroar_tracer_preload.so"
            fi
            # ROAR_PROXY_PORT is injected via env_vars; set by RoarNodeAgent
            # after init. Workers wait briefly for the agent to be ready.
            if [ -n "$ROAR_PROXY_PORT" ]; then
                export AWS_ENDPOINT_URL="http://127.0.0.1:$ROAR_PROXY_PORT"
            fi
            exec python3 "$@"
        """).strip())
    os.chmod(wrapper, 0o755)

    # Merge with existing working_dir if present (copy user files into tmp_dir)
    existing_working_dir = runtime_env.get("working_dir")
    if existing_working_dir and os.path.isdir(existing_working_dir):
        for item in os.listdir(existing_working_dir):
            src = os.path.join(existing_working_dir, item)
            dst = os.path.join(tmp_dir, item)
            if not os.path.exists(dst):
                shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)

    runtime_env["working_dir"] = tmp_dir
    runtime_env["py_executable"] = "./roar_worker_wrapper.sh"
    return runtime_env
```

**Why `working_dir` is the right mechanism:**
- It's distributed to every node automatically by Ray.
- Workers start in their node's copy of the directory — `./` references work.
- The preload `.so` travels with the job, no installation step required.
- `py_executable` is documented as experimental but actively used by the `uv`
  integration, so it's stable enough for our purposes.

**Proxy port discovery:** The wrapper reads `$ROAR_PROXY_PORT` from env vars.
`RoarNodeAgent` sets this on its node by writing to a well-known location (a
small JSON file in the working_dir copy or via a named actor). Workers briefly
poll for readiness on startup (a few seconds). If no agent is ready, they
proceed without the proxy (falling back to boto3 monkey-patching).

**`working_dir` conflict handling:** If the user already has `working_dir` set
to a remote URI (S3/GitHub), we cannot merge it. In that case, fall back to the
Python-level instrumentation only and log a warning.

---

### 4. `sitecustomize.py` Update

`_roar_ray_init` is updated in two ways:

**Before `ray.init()`** — generate the wrapper script and inject `py_executable`:

```python
def _roar_ray_init(*args, **kwargs):
    runtime_env = dict(kwargs.pop("runtime_env", None) or {})
    env_vars = dict(runtime_env.get("env_vars", {}) or {})

    # Existing injections
    env_vars["ROAR_WORKER"] = "1"
    env_vars["ROAR_JOB_ID"] = job_id
    env_vars["ROAR_LOG_DIR"] = log_dir
    env_vars["ROAR_PROXY_PORT"] = "0"   # placeholder; wrapper discovers at runtime

    # NEW: inject py_executable wrapper
    wrapper_path = _write_worker_wrapper(log_dir)
    if wrapper_path:
        runtime_env["py_executable"] = wrapper_path

    runtime_env["env_vars"] = env_vars
    runtime_env["worker_process_setup_hook"] = "roar.ray.worker:setup"
    kwargs["runtime_env"] = runtime_env

    result = _real_ray_init(*args, **kwargs)

    # After init: spawn node agents on all alive nodes
    _spawn_node_agents(job_id, log_dir)
    return result
```

**`_write_worker_wrapper()`** writes a shell script to a temp dir and returns
its path. The script discovers the local preload lib and proxy port at runtime,
so a single script template works on all nodes.

**After `ray.init()`** — spawn node agents non-blocking. Workers start
immediately via the wrapper (which queries the local agent for the proxy port).
If the agent isn't ready yet, the wrapper waits up to 5 seconds.

---

### 5. `ray.shutdown` Patch

Patch `ray.shutdown` the same way we patch `ray.init` — collect all node agent
logs before Ray tears down:

```python
def _patch_ray_shutdown(ray_module) -> None:
    _real_shutdown = ray_module.shutdown

    def _roar_shutdown(*args, **kwargs):
        _collect_all_node_agents()   # collect before shutdown
        return _real_shutdown(*args, **kwargs)

    ray_module.shutdown = _roar_shutdown
```

---

### 6. Collector Update (`collector.py`)

Extend the existing `collect()` to also process native tracer logs from node
agents, in addition to the Python-level actor logs. The existing `_write_to_db`
logic already handles the `capture_method` field — tracer logs get
`capture_method="tracer"`, Python-level logs get `"python"`, proxy logs get
`"proxy"`. The collector deduplicates by path and picks the highest-priority
capture method (tracer > proxy > python).

---

## Log Formats

| Source | Format | Content |
|--------|--------|---------|
| Preload tracer | msgpack | syscall-level file events: `{path, op, pid, ts, task_id}` |
| S3 proxy | text lines | `[S3:PutObject] s3://bucket/key etag=...` |
| Python sitecustomize | JSONL | `{path, mode, task_id, node_id, ts}` |

The collector already parses proxy log lines (`parse_log_line()`) and JSONL.
Add msgpack parsing for tracer logs using the existing format from
`ExecutionJobRecorder`.

---

## Fallback Behavior

| Condition | Behavior |
|-----------|---------|
| Preload lib not found on node | Worker falls back to Python-level tracking |
| Proxy binary not found on node | S3 tracking via boto3 monkey-patch only |
| Node agent unreachable at setup | Worker skips native tracing, logs warning |
| eBPF available | `roard` daemon started by agent instead of preload (agent auto-detects) |
| ray.shutdown called before agents ready | Collect whatever is available, warn on missing |

---

## Open Questions

1. **`working_dir` conflict with remote URIs:** If the user sets `working_dir`
   to an S3 or GitHub URI, we cannot merge our files into it. In that case,
   fall back to Python-level instrumentation and log a warning. Future work:
   download the remote working_dir, merge, re-upload to S3.

2. **`py_executable` is experimental:** Ray marks this as experimental. If it
   breaks on a particular Ray version, we need a fallback. The fallback is the
   existing `worker_process_setup_hook` + Python-level patching.

3. **eBPF on remote nodes:** If `roard` is available and `CAP_BPF` is
   granted, the agent should prefer eBPF over preload. Agent auto-detects
   using existing `_ebpf_is_ready()` logic from `tracer_backends`.

4. **`ROAR_TRACER_OUTPUT_FILE` per task vs per worker:** The preload tracer
   may write to a single output file per process. With worker reuse, multiple
   tasks share a worker process. Check if the tracer supports per-task rotation
   via env var or signal; if not, post-process using timestamps to attribute
   events to tasks.

5. **Port conflicts on multi-agent hosts:** Multiple node agents on the same
   physical host (e.g. local multi-node simulation) must not bind the same
   proxy port. `_find_free_port()` must bind-test before returning.

6. **`working_dir` size limit:** Ray enforces a 500 MiB limit on `working_dir`
   uploads. The preload `.so` is typically <5 MB, well within limits.

---

## Implementation Order

1. `RoarNodeAgent` + proxy startup (`roar/ray/node_agent.py`)
2. `_spawn_node_agents` in sitecustomize post-init
3. `ray.shutdown` patch in sitecustomize
4. Worker setup hook: `_apply_native_tracer(agent)`
5. Collector: add msgpack tracer log parsing + node agent collection
6. TDD tests:
   - Unit: agent startup, proxy port discovery, preload lib discovery
   - E2E: `test_native_tracing.py` — verify tracer-captured events appear
     in roar.db alongside Python-level events; verify `capture_method="tracer"`
     for at least one artifact

---

## File Layout Changes

```
roar/
└── ray/
    ├── node_agent.py       ← NEW: RoarNodeAgent actor
    ├── actor.py            ← existing log collector actor
    ├── worker.py           ← updated: _apply_native_tracer()
    └── collector.py        ← updated: msgpack parsing, node agent collection

roar/services/execution/
    └── sitecustomize.py    ← updated: _spawn_node_agents(), ray.shutdown patch
```
