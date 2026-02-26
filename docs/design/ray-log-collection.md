# Ray Log Collection: Distributed Design

## Problem

The current implementation writes worker I/O logs to `ROAR_LOG_DIR` (a filesystem path),
then reads them from the driver at job completion. This works in Docker (shared volume) but
**breaks on any real Ray cluster** where workers run on separate machines with no shared
filesystem.

```
Current (broken on real clusters):

  Worker node A          Worker node B          Driver
  ─────────────          ─────────────          ──────
  writes to              writes to              reads from
  /shared/logs/ ────┐    /shared/logs/ ────┐   /shared/logs/
                    └──────────────────────┘   ← this path doesn't exist
                                               on the driver!
```

---

## Options

### Option A — Ray Actor Aggregator (recommended)

A lightweight `@ray.remote(num_cpus=0)` actor acts as an in-cluster log buffer.
Workers send events to it via Ray's object store (which IS shared across the
cluster). Driver collects from the actor at shutdown.

```
  Worker node A          Worker node B          Driver
  ─────────────          ─────────────          ──────
  append_batch()─────┐   append_batch()─────┐   get_all() → merge → DB
                     ▼                      ▼
              ┌──────────────────────────────────────┐
              │  RoarLogCollectorActor (detached)    │
              │  namespace="roar"                    │
              │  name="roar-log-collector-<job_id>"  │
              └──────────────────────────────────────┘
                       (runs anywhere in cluster)
```

**Pros:** No shared filesystem, no external deps, works on any Ray cluster,
events survive worker crashes (already flushed to actor), Ray manages lifecycle.

**Cons:** Actor holds events in memory — could grow large on very long jobs.
Mitigation: batch flushes + periodic `ray.put()` snapshots for large jobs.

---

### Option B — HTTP Collection

Driver exposes an HTTP endpoint; workers POST events.

**Pros:** Simple protocol, low per-event overhead.

**Cons:** Driver IP must be reachable from all worker nodes (breaks with NAT,
VPCs without proper routing, Kubernetes pods on different subnets). Too fragile.

---

### Option C — S3 / Object Storage Buffer

Workers write log batches to a configured S3 prefix; driver reads at shutdown.

**Pros:** Persistent, scales to any job size, works anywhere S3 is accessible.

**Cons:** Requires S3 credentials on all worker nodes, not zero-config,
per-event S3 write cost is too high (must batch).

**Verdict:** Good future option for very large jobs, but not the right default.

---

## Recommended: Tiered Backend

| Tier | Condition | Backend |
|------|-----------|---------|
| 1 (default) | Ray is initialized | Actor aggregator |
| 2 (opt-in) | `[ray] log_backend = "s3"` in config | S3 buffer |
| 0 (legacy) | `ROAR_LOG_DIR` is writable on both sides | Filesystem (current) |

Auto-detection at worker setup time:
```python
def _choose_backend() -> str:
    if _shared_fs_available():
        return "filesystem"   # local dev / Docker with shared volume
    return "actor"            # real cluster, default
```

`_shared_fs_available()` = try `os.makedirs(log_dir)` + write a sentinel file;
if it succeeds, assume filesystem mode. Opt-out via `ROAR_LOG_BACKEND=actor`.

---

## Actor Design

### `RoarLogCollectorActor` (`roar/ray/actor.py`)

```python
@ray.remote(num_cpus=0, max_concurrency=500)
class RoarLogCollectorActor:
    def __init__(self) -> None:
        self._events: list[dict] = []

    def append_batch(self, events: list[dict]) -> None:
        self._events.extend(events)

    def get_all(self) -> list[dict]:
        return list(self._events)
```

- **Placement:** No placement constraint — Ray schedules it anywhere.
  `num_cpus=0` avoids consuming compute resources.
- **Naming:** `roar-log-collector-{ROAR_JOB_ID}` in namespace `"roar"`.
- **Lifetime:** `"detached"` — survives individual task failures.
- **Concurrency:** `max_concurrency=500` — handles burst writes from many workers.

---

## Worker Changes (`roar/ray/worker.py`)

### Startup (`setup()`)

```python
_BACKEND: str = "filesystem"   # set once at setup time
_actor = None                  # cached actor handle
_event_buffer: list[dict] = [] # thread-local batch buffer
_FLUSH_THRESHOLD = 50          # events before auto-flush

def setup() -> None:
    global _BACKEND
    _BACKEND = _choose_backend()
    if _BACKEND == "actor":
        _init_actor()
    else:
        os.makedirs(_LOG_DIR, exist_ok=True)
    builtins.open = _tracking_open
    # ... existing patches ...
```

### Get or Create Actor

```python
def _init_actor() -> None:
    global _actor
    import ray
    job_id = os.environ.get("ROAR_JOB_ID", "default")
    name = f"roar-log-collector-{job_id}"
    try:
        _actor = ray.get_actor(name, namespace="roar")
    except ValueError:
        from roar.ray.actor import RoarLogCollectorActor
        _actor = RoarLogCollectorActor.options(
            name=name,
            namespace="roar",
            lifetime="detached",
            num_cpus=0,
        ).remote()
```

Note: Multiple workers may race to create the actor simultaneously. Ray handles
named actor creation atomically — only one will succeed, others will get the
existing handle from `get_actor()`. The worker should catch and retry on the
race condition.

### `_log_access()` — buffered fire-and-forget

```python
def _log_access(path, mode, **kwargs) -> None:
    task_id, node_id = _runtime_context_ids()
    if not task_id:
        return

    payload = {"path": path, "mode": mode, "task_id": task_id, "ts": time.time(), ...}

    if _BACKEND == "actor" and _actor is not None:
        _event_buffer.append(payload)
        if len(_event_buffer) >= _FLUSH_THRESHOLD:
            _flush_to_actor()
    else:
        _write_to_file(task_id, payload)   # existing filesystem path


def _flush_to_actor() -> None:
    if not _event_buffer or _actor is None:
        return
    batch = list(_event_buffer)
    _event_buffer.clear()
    _actor.append_batch.remote(batch)   # fire-and-forget
```

**Final flush:** Register an `atexit` handler in `setup()` to flush any
remaining buffered events when the worker process exits.

---

## Collector Changes (`roar/ray/collector.py`)

```python
def collect(project_dir=None, log_dir=None) -> None:
    events = _collect_events(log_dir)
    if not events:
        return
    _write_to_db(project_dir, events)


def _collect_events(log_dir) -> list[dict]:
    # Try actor first
    try:
        import ray
        if ray.is_initialized():
            events = _collect_from_actor()
            if events is not None:
                return events
    except Exception:
        pass

    # Fall back to filesystem
    return _collect_from_filesystem(log_dir)


def _collect_from_actor() -> list[dict] | None:
    import ray
    job_id = os.environ.get("ROAR_JOB_ID", "default")
    name = f"roar-log-collector-{job_id}"
    try:
        actor = ray.get_actor(name, namespace="roar")
        events = ray.get(actor.get_all.remote(), timeout=30)
        ray.kill(actor)
        return events
    except Exception:
        return None   # actor doesn't exist or unreachable — fall through
```

---

## ROAR_JOB_ID Propagation

The actor name includes `ROAR_JOB_ID` so multiple concurrent roar jobs don't
share a single actor. This env var must be:

1. Generated by the driver at job start
2. Injected into `runtime_env.env_vars` alongside `ROAR_WORKER=1`

In `sitecustomize.py`'s `_roar_ray_init()`:
```python
import uuid
job_id = str(uuid.uuid4())[:8]
env_vars["ROAR_JOB_ID"] = job_id
```

---

## Migration

The filesystem path is preserved as the Tier 0 fallback. Docker-based e2e tests
continue to work unchanged. The actor backend activates automatically when the
shared filesystem sentinel write fails.

To force actor mode in tests: `ROAR_LOG_BACKEND=actor`.
To force filesystem mode: `ROAR_LOG_BACKEND=filesystem`.

---

## Open Questions

1. **Actor memory ceiling:** For jobs with millions of file events, the actor
   could exhaust memory. Mitigation options:
   - Periodic `ray.put()` snapshots from the actor (stores batches in object store)
   - Cap events per actor, spill to S3 if configured
   - For now: document the limit; address in a follow-up with S3 backend

2. **Actor placement:** Should the actor run on the head node (more reliable) or
   anywhere (Ray default)? Head node placement requires a resource label.
   Default (anywhere) is simpler and sufficient for v1.

3. **Concurrent job isolation:** `ROAR_JOB_ID` separates actors per job, but
   if the same worker process is reused across jobs (Ray worker reuse), the
   actor handle cache (`_actor`) must be invalidated per job. Use
   `ROAR_JOB_ID` as the cache key.

4. **Ray not initialized on driver at atexit:** If the user calls `ray.shutdown()`
   before roar's atexit fires, `ray.get_actor()` will fail. Solution: hook into
   Ray shutdown directly, or collect before `ray.shutdown()` is called by patching
   `ray.shutdown` the same way we patch `ray.init`.

---

## Implementation Plan

1. **`roar/ray/actor.py`** — `RoarLogCollectorActor` class
2. **`roar/ray/worker.py`** — backend detection, actor init, buffered flush
3. **`roar/ray/collector.py`** — actor collection path, filesystem fallback
4. **`roar/services/execution/inject/sitecustomize.py`** — inject `ROAR_JOB_ID`
5. **Tests:**
   - Unit: actor creation race condition, backend detection, buffer flushing
   - E2E: new test that explicitly sets `ROAR_LOG_BACKEND=actor` and verifies
     collection works without a shared volume
