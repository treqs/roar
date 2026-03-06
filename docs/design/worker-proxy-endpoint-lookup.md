# Worker Proxy Endpoint Lookup — Design Decision

**Date:** 2026-03-06
**Status:** Decided — Option A (lazy init)
**Author:** Rex (with Trevor)

## Problem

`roar_worker._startup()` is called from Ray's `worker_process_setup_hook`, which
fires before the CoreWorker is fully initialized. The function calls
`ray.get_actor()` to look up the node agent and get the proxy port. This triggers
a GCS RPC (`GetNamedActorInfo`) that segfaults in C++ — uncatchable by Python's
try/except.

**Crash chain:**
```
worker_process_setup_hook
  → _startup()
    → _configure_local_proxy_endpoint()
      → ray.get_actor(agent_name, namespace="roar")
        → CoreWorker::GetNamedActorHandle()
          → GcsRpcClient::GetNamedActorInfo()
            → SIGSEGV (CoreWorker not initialized)
```

**Reproduced:** `tests/e2e/ray/test_setup_hook_crash.py` (commit `944bc97`)

## Decision: Option A — Lazy init on first `open()` call

Move `_configure_local_proxy_endpoint()` out of `_startup()`. Call it lazily on
the first invocation of `_tracking_open()`, guarded by a `_proxy_configured` flag.
By the time a task calls `open()`, the CoreWorker is fully initialized and
`ray.get_actor()` is safe.

### What changes
- `_startup()` no longer calls `_configure_local_proxy_endpoint()`
- `_tracking_open()` calls `_configure_local_proxy_endpoint()` once on first use
- Add `_proxy_configured: bool = False` module global

### Trade-offs
- **+** Minimal change (~10 lines)
- **+** CoreWorker guaranteed ready when tasks execute
- **+** No architectural changes
- **-** First `open()` per worker takes ~10ms extra (one-time actor lookup)
- **-** Still depends on `ray.get_actor()` GCS RPC (fragile under GCS load)

## Alternatives Considered

### Option B: Environment variable for proxy port
Inject `ROAR_PROXY_PORT` into runtime_env env_vars. Workers read `os.environ`.

- **+** Zero GCS dependency in workers
- **+** Fast, deterministic
- **-** Port not known at submission time (node agent starts after job submit)
- **-** Would need file-based approach, introducing race conditions

### Option C: Shared tmp file with retry
Node agent writes port to `/tmp/roar-proxy-{job_id}-{node_id}.port`. Workers
poll for the file with short timeout.

- **+** No GCS calls, no segfault risk, works in setup hook
- **+** Decouples workers from Ray actor system
- **-** Requires node agent to run on same physical node (needs `NodeAffinitySchedulingStrategy`)
- **-** Race condition: worker starts before agent writes file
- **-** More moving parts

**This is the recommended long-term direction** — eliminates GCS dependency entirely.

### Option D: Driver injects per-node port env vars
`sitecustomize.py` injects `ROAR_PROXY_PORT_{NODE_ID}` into runtime_env.

- **+** Cleanest — no GCS, no files, no retries
- **-** Runtime env is fixed at submission time; can't add per-node vars dynamically
- **-** Doesn't work with autoscaling (new nodes unknown at submit time)
