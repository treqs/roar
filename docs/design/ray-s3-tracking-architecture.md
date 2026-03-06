# Ray S3 Tracking Architecture

## Overview

roar tracks S3 operations (reads/writes) during pipeline execution to build a lineage graph of artifacts. On a Ray cluster, this must work across multiple nodes where the driver process and worker processes run on different machines.

## The S3 Proxy (Primary Mechanism)

`roar-proxy` is a Rust binary that acts as an S3-compatible HTTP reverse proxy. It forwards all requests to the real S3 endpoint while logging every operation (GetObject, PutObject, etc.) with bucket, key, ETag, and size.

**Why this is the right approach:**
- Captures ALL S3 traffic regardless of SDK, method, or library (boto3, botocore, awscli, direct HTTP)
- Zero SDK-level monkey-patching → no latency from Python-level interception
- Works with `boto3.client()`, `boto3.Session().client()`, `boto3.resource()`, or any other AWS SDK
- Single point of capture → no gaps from missing patch targets

**How it works:**
```
Application → AWS_ENDPOINT_URL=http://127.0.0.1:<port> → roar-proxy → real S3
                                                              ↓
                                                         logs operations
```

The proxy is activated by setting `AWS_ENDPOINT_URL` in the process environment. The AWS SDK honors this variable and routes all service calls through it.

## Architecture Layers

### Layer 1: Local `roar run` (Single Machine)

File: `roar/services/execution/coordinator.py`

```
roar run python train.py
  → RunCoordinator starts roar-proxy on a free port
  → Sets AWS_ENDPOINT_URL=http://127.0.0.1:<port> in child env
  → Runs the user command
  → On exit: stops proxy, parses log lines → S3LogEntry list
  → Registers S3LogEntries as artifacts in .roar/roar.db
```

Controlled by `config.toml`: `proxy.enabled = true`

### Layer 2: Ray Driver (sitecustomize.py)

File: `roar/services/execution/inject/sitecustomize.py`

When a script calls `ray.init()`, sitecustomize intercepts and:

**Path A — Direct usage (no sentinel):**
1. Merges `runtime_env` with roar's pip, worker_process_setup_hook, env vars
2. Propagates `AWS_ENDPOINT_URL` from driver env to worker env vars
3. Spawns `RoarNodeAgent` actors per node (if `ROAR_RAY_NODE_AGENTS=1`)
4. On `ray.shutdown()`: collects proxy logs from all node agents, registers artifacts

**Path B — Pre-instrumented (`ROAR_JOB_INSTRUMENTED=1`):**
1. Submission side (`_ray_job_submit.py`) already injected pip, worker hook, env vars
2. Driver's `ray.init()` sees the sentinel → takes early return
3. Currently: skips node agent spawning entirely ← **THIS IS A GAP**
4. Workers fall back to boto3 monkey-patching only

### Layer 3: Ray Node Agent (Per-Node Proxy)

File: `roar/ray/node_agent.py`

`RoarNodeAgent` is a Ray actor deployed once per cluster node. Each agent:
1. Finds the `roar-proxy` binary (shipped in the roar-cli wheel at `roar/bin/roar-proxy`)
2. Starts it on a free port on that node
3. Exposes `get_proxy_port()` for workers to discover the port
4. On shutdown: terminates proxy, returns collected log lines

### Layer 4: Ray Worker (Setup Hook)

**Old worker** (`roar/ray/worker.py`) — pre-Phase 2:
- `_configure_local_proxy_endpoint()`: looks up the node's `RoarNodeAgent` actor, gets proxy port, sets `AWS_ENDPOINT_URL`
- `_patch_boto3()`: monkey-patches `boto3.client()` as a fallback
- Result: S3 ops route through the per-node proxy → full capture

**New worker** (`roar/ray/roar_worker.py`) — Phase 2 (fragments-only):
- `_startup()`: patches `builtins.open`, calls `_patch_boto3()`, registers atexit handlers
- **Does NOT call `_configure_local_proxy_endpoint()`** ← **GAP**
- `_patch_boto3()`: only wrapped `boto3.client()`, not `boto3.Session.client()` ← **FIXED in `4664e0e`**
- `_emit_fragment()`: sends captured I/O to `GlaasFragmentStreamer` ← **FIXED in `98cc07e`**

### Layer 5: Fragment Collection (Cloud Path)

File: `roar/ray/glaas_fragment_streamer.py`, `roar/ray/fragment_reconstituter.py`

For remote clusters where the local DB isn't accessible:
1. Submission side registers a fragment session with GLaaS
2. Workers encrypt and POST fragment batches to GLaaS
3. After job completion, the local side fetches + decrypts fragments
4. Fragments are merged into the local `.roar/roar.db`

## Identified Gaps

### Gap 1: `ROAR_JOB_INSTRUMENTED` Path Skips Node Agents (CRITICAL)

**Location:** `sitecustomize.py` lines 264-281

When `roar run ray job submit` sets `ROAR_JOB_INSTRUMENTED=1`, the driver's `ray.init()` takes an early return that:
- ✅ Creates the collector actor
- ❌ Does NOT check `_node_agents_enabled()`
- ❌ Does NOT spawn `RoarNodeAgent` actors
- ❌ Does NOT start the node poller

**Impact:** On a remote cluster with the sentinel set, no per-node proxies are started. Workers have no `AWS_ENDPOINT_URL` override. S3 tracking falls back to boto3 monkey-patching only.

**Fix:** The sentinel path should still spawn node agents if `ROAR_RAY_NODE_AGENTS=1` (or if a new default-on config flag enables it). The submission side (`_ray_job_submit.py`) should inject `ROAR_RAY_NODE_AGENTS=1` into the runtime env.

### Gap 2: `roar_worker.py` Missing `_configure_local_proxy_endpoint()` (CRITICAL)

**Location:** `roar/ray/roar_worker.py` — `_startup()` function

The old `worker.py` had `_configure_local_proxy_endpoint()` which:
1. Looks up the `RoarNodeAgent` actor for the current node
2. Gets the proxy port
3. Sets `AWS_ENDPOINT_URL=http://127.0.0.1:<port>`

The new `roar_worker.py` (Phase 2) does not have this function. Even if node agents are spawned (fixing Gap 1), workers won't discover or use the proxy.

**Fix:** Port `_configure_local_proxy_endpoint()` from `worker.py` to `roar_worker.py` and call it in `_startup()` before `_patch_boto3()`.

### Gap 3: `_ray_job_submit.py` Doesn't Inject Node Agent Config (MODERATE)

**Location:** `roar/cli/commands/_ray_job_submit.py`

The submission side injects `ROAR_JOB_INSTRUMENTED`, `ROAR_SESSION_ID`, `ROAR_FRAGMENT_TOKEN`, `GLAAS_URL` into env vars. But it does NOT inject `ROAR_RAY_NODE_AGENTS=1`.

Without this, even if the sentinel path is fixed (Gap 1), node agents won't be enabled unless the user manually sets the env var.

**Fix:** `_ray_job_submit.py` should inject `ROAR_RAY_NODE_AGENTS=1` into the runtime env by default for remote cluster submissions.

### Gap 4: boto3 Monkey-Patching Gaps (LOW — Defense in Depth)

**Location:** `roar/ray/roar_worker.py` — `_patch_boto3()`

The boto3 patching is a fallback for when the proxy isn't available. It had a gap where `boto3.Session.client()` wasn't patched (fixed in `4664e0e`). This adds latency compared to the proxy approach and should remain a fallback only.

**Status:** Fixed, but should be clearly documented as defense-in-depth, not primary.

### Gap 5: Proxy Logs Not Integrated with Fragment Streaming (MODERATE)

**Location:** `roar/ray/node_agent.py`, `roar/ray/roar_worker.py`

Currently, proxy logs are collected by the driver via `_collect_node_agent_logs()` during `ray.shutdown()`. But in the fragment-only flow (Phase 2), the driver may not have the ability to collect logs from node agents (especially on remote clusters where the driver is the Ray JobSupervisor, not the user's machine).

**Questions to resolve:**
- Should node agent proxy logs be converted to fragments and streamed to GLaaS?
- Or should the worker read its own node agent's logs and include S3 ops in its fragments?
- Or should the proxy log entries be sent directly to the fragment streamer from the node agent actor?

### Gap 6: `_ensure_collector_actor` Still Called in Sentinel Path (CLEANUP)

**Location:** `sitecustomize.py` line 281

The sentinel path calls `_ensure_collector_actor()` but Phase 2 removed the collector actor. This is dead code that should be cleaned up.

## Correct End-to-End Flow (After Fixes)

```
Developer machine:
  roar run ray job submit --address <cluster> -- python main.py
    ↓
  _ray_job_submit.py:
    - Registers fragment session with GLaaS
    - Injects into runtime_env:
      - pip: [roar-cli wheel]
      - worker_process_setup_hook: roar.ray.roar_worker._startup
      - env_vars: ROAR_JOB_INSTRUMENTED=1, ROAR_SESSION_ID, ROAR_FRAGMENT_TOKEN,
                  GLAAS_URL, ROAR_RAY_NODE_AGENTS=1
    - Submits to Ray Jobs API
    - Waits for completion
    - Reconstitutes fragments from GLaaS into local .roar/roar.db

Cluster (driver / JobSupervisor):
  python main.py
    ↓
  ray.init() intercepted by sitecustomize.py:
    - Sees ROAR_JOB_INSTRUMENTED=1
    - Sees ROAR_RAY_NODE_AGENTS=1
    - Spawns RoarNodeAgent actor per node (each starts roar-proxy)
    - Does NOT re-inject pip/env vars (already done by submission side)

Cluster (each worker process):
  worker_process_setup_hook → roar.ray.roar_worker._startup():
    1. _configure_local_proxy_endpoint():
       - Discovers RoarNodeAgent for this node
       - Gets proxy port
       - Sets AWS_ENDPOINT_URL=http://127.0.0.1:<port>
    2. _patch_boto3() (defense-in-depth fallback)
    3. builtins.open patched for file I/O tracking
    4. atexit: flush fragments to GLaaS

  Worker executes Ray task:
    - S3 calls → AWS_ENDPOINT_URL → roar-proxy on this node → real S3
    - Proxy logs: GetObject s3://bucket/key, PutObject s3://bucket/key
    - File I/O: tracked via builtins.open patch
    - Fragment emitted with reads/writes lists

  On task completion:
    - Fragment includes S3 paths (from proxy) + file paths (from open patch)
    - GlaasFragmentStreamer encrypts and POSTs to GLaaS

Developer machine (after job completion):
  FragmentReconstituter:
    - Fetches encrypted batches from GLaaS
    - Decrypts with local key
    - Merges into .roar/roar.db as jobs + artifacts
```

## Open Questions

1. **How do proxy logs reach fragments?** The proxy runs as a separate process — its log lines aren't automatically available to the Python worker process. Options:
   a. Worker queries the node agent actor for proxy logs at task boundaries
   b. Node agent actor itself emits fragments with S3 entries
   c. Proxy writes to a shared file that the worker reads

2. **Should `ROAR_RAY_NODE_AGENTS` default to `1`?** Currently opt-in. For cloud clusters, it should probably be the default since the proxy is the only reliable S3 tracking mechanism.

3. **Does the proxy binary work on all target platforms?** The wheel includes an x86_64 Linux binary. ARM clusters (Graviton) would need a separate build.

4. **Proxy → fragment integration**: The current `roar_worker.py` emits fragments with reads/writes from the Python hooks. Proxy-captured S3 ops need to flow into the same fragment structure. This may require the worker to periodically query the node agent for new proxy log entries, or the node agent to push entries to the worker.

## File Reference

| File | Role |
|------|------|
| `roar/services/execution/proxy.py` | ProxyService — manages roar-proxy binary lifecycle |
| `roar/services/execution/coordinator.py` | RunCoordinator — orchestrates local `roar run` |
| `roar/services/execution/inject/sitecustomize.py` | Ray integration — intercepts `ray.init()` |
| `roar/cli/commands/_ray_job_submit.py` | Submission-side rewriting for `roar run ray job submit` |
| `roar/ray/node_agent.py` | RoarNodeAgent — per-node proxy actor |
| `roar/ray/worker.py` | Old worker (pre-Phase 2) — has `_configure_local_proxy_endpoint()` |
| `roar/ray/roar_worker.py` | New worker (Phase 2) — missing proxy endpoint config |
| `roar/ray/glaas_fragment_streamer.py` | Encrypts and streams fragments to GLaaS |
| `roar/ray/fragment_reconstituter.py` | Fetches and decrypts fragments from GLaaS |
| `roar/ray/fragment_key.py` | Fragment session key management |
| `roar/bin/roar-proxy` | The proxy binary (shipped in wheel, ~19MB) |
