# Option C: `roar-worker` Entry Point — DAG Reconstruction & GLaaS Registration Design

**Status:** Draft for review  
**Author:** Rex  
**Date:** 2026-02-26  
**Depends on:** `docs/design/ray-worker-lineage.md`, `docs/design/ray-log-collection.md`

---

## Goal

Enable `roar run python train.py` (where `train.py` uses Ray) to produce a **complete,
registerable lineage graph** in GLaaS — one that includes every Ray worker task as a
first-class job node, with content-addressed artifacts, per-task attribution, and full
reproducibility — without requiring the user to change their Ray code.

From `~/dev/ROAR_GOALS.md`:
> Make reproducibility default with content-based artifact tracking and replayable lineage.

---

## How Registration Works Today (Non-Ray)

Understanding the current registration chain is essential context before designing the
distributed extension.

### Local DB → GLaaS flow

```
roar run python train.py
  │
  ├─ [local] creates Session (sessions table)
  ├─ [local] creates Job (jobs table, job_uid = random 8-char hex)
  ├─ [local] records artifacts (artifacts + artifact_hashes tables, content-addressed by blake3)
  ├─ [local] records job_inputs / job_outputs (lineage edges)
  │
roar register model.pt
  │
  ├─ 1. hash model.pt → blake3 digest
  ├─ 2. look up artifact in local DB → find producing job(s)
  ├─ 3. LineageCollector walks backwards: job → inputs → upstream jobs → their inputs...
  ├─ 4. compute session_hash (content hash of ordered step_identity values)
  ├─ 5. detect + filter secrets
  ├─ 6. POST /api/v1/sessions  { hash, git_repo, git_commit, git_branch }
  ├─ 7. POST /api/v1/artifacts (batch) — register all artifacts in lineage
  ├─ 8. POST /api/v1/sessions/{session_hash}/jobs  — register each job
  └─ 9. POST /api/v1/sessions/{session_hash}/jobs/{job_uid}/inputs|outputs
```

### Key identifiers

| Identifier | What it is | How it's computed |
|---|---|---|
| `artifact_id` | Local UUID for an artifact row | `uuid4()` on first insert |
| `blake3 digest` | Content address | `blake3(file_bytes)` |
| `job_uid` | Unique job identifier | 8-char random hex, used in GLaaS URLs |
| `step_identity` | Dedup key for a step | `hash(command + sorted(input_hashes))` |
| `session_hash` | Content hash of the pipeline | `hash(sorted(step_identity values))` |

### The DAG model

GLaaS stores a **bipartite DAG**: jobs and artifacts alternate as nodes, with directed
edges (inputs → job → outputs). Given an artifact hash, `GET /api/v1/artifacts/{hash}/dag`
walks the DAG backwards to return the full reproduction plan.

```
dataset.parquet ──┐
                  ├──► [job: preprocess.py] ──► features.parquet ──┐
config.yaml ──────┘                                                  ├──► [job: train.py] ──► model.pt
                                                                     │
                                               base_model.pt ────────┘
```

The session provides ordering (step_number) and git context (commit, branch).

---

## The Distributed Challenge

When `train.py` uses Ray, the actual computation is:

```
roar run python train.py  (driver process)
  │
  └─ ray.init()
       │
       ├─ Worker 1: task T001 reads shard_0.parquet → writes checkpoint_0.pt
       ├─ Worker 2: task T002 reads shard_1.parquet → writes checkpoint_1.pt
       ├─ Worker 3: task T003 reads shard_2.parquet → writes checkpoint_2.pt
       │
  └─ driver aggregates checkpoints → final_model.pt
  └─ ray.shutdown()
```

In the current non-distributed model, all of this collapses to one job:
`job: "python train.py"` → inputs: [shard_0, shard_1, shard_2] → outputs: [final_model.pt]

Worker-level intermediate artifacts and per-task provenance are invisible. This is adequate
for some use cases but insufficient for:
- Reproducing a single worker's output in isolation
- Diagnosing which shard caused a training failure
- Understanding checkpoint lineage at the task level
- GLaaS search: "find all runs that read shard_1.parquet"

---

## Proposed: Per-Task Job Nodes with Parent Linkage

### Core idea: every Ray task is a job

Each Ray task invocation gets its own `job_uid`. Tasks are linked to their parent driver job
via a new `parent_job_uid` field. The local DB and GLaaS both understand parent-child job
relationships.

```
Session: "train run 2026-02-26"
  step @1: Job "python train.py"  [job_uid: "a1b2c3d4"]  ← driver job
    child: Job "ray_task:preprocess_shard"  [job_uid: "t001xxxx", parent: "a1b2c3d4"]
    child: Job "ray_task:preprocess_shard"  [job_uid: "t002xxxx", parent: "a1b2c3d4"]
    child: Job "ray_task:train_shard"       [job_uid: "t003xxxx", parent: "a1b2c3d4"]
```

### Schema change: `parent_job_uid` on jobs

```sql
ALTER TABLE jobs ADD COLUMN parent_job_uid TEXT REFERENCES jobs(job_uid);
```

One column addition, backwards compatible. Non-Ray jobs have `parent_job_uid = NULL`.

### GLaaS API extension: `parent_job_uid` in job registration

```json
POST /api/v1/sessions/{session_hash}/jobs
{
  "command":          "ray_task:train_shard",
  "timestamp":        1740582413.2,
  "job_uid":          "t003xxxx",
  "parent_job_uid":   "a1b2c3d4",     ← new field
  "job_type":         "ray_task",      ← new job_type
  "duration_seconds": 42.1,
  "exit_code":        0,
  "git_commit":       "abc123",
  "git_branch":       "main",
  "step_number":      1                ← same as parent (parallel to driver)
}
```

The `parent_job_uid` field allows GLaaS to:
1. Group child tasks under their parent in the DAG view
2. Collapse task nodes into a summary when rendering large distributed jobs
3. Reproduce a single task in isolation (given its inputs and command)

**Backwards compatibility:** GLaaS can ignore `parent_job_uid` if not implemented yet
(the field is additive). The lineage still works — child tasks just appear as sibling jobs
in the session rather than nested under the driver.

---

## roar-worker: The Fragment Producer

`roar-worker` is a new Python entry point installed by `roar-cli`. It replaces the current
`worker_process_setup_hook` + `py_executable` wrapper approach.

### Invocation

When `sitecustomize.py` patches `ray.init`, it sets:

```python
runtime_env["py_executable"] = "roar-worker"
runtime_env["worker_process_setup_hook"] = None   # replaced by roar-worker
```

Ray calls it as: `roar-worker <python_args>`

`roar-worker` is a long-lived process (Ray worker pool reuse). It starts once per worker
process and handles many task invocations.

### What roar-worker does

```
roar-worker starts (once per worker process)
  │
  ├─ Reads ROAR_JOB_ID, ROAR_DRIVER_JOB_UID from env     ← set by driver's ray.init patch
  ├─ Sets up file I/O tracking (patches builtins.open)
  ├─ Sets up S3 tracking (patches boto3)
  ├─ Sets up LD_PRELOAD (libroar_tracer_preload.so if present)
  ├─ Connects to RoarLogCollectorActor (via ROAR_JOB_ID)
  │
  └─ For each task invocation (Ray calls into the worker process repeatedly):
       ├─ Detects task start: ray.get_runtime_context().get_task_id() changes
       ├─ Opens a new task-scoped fragment:
       │    task_uid = new_uid_from(ray_task_id)   ← deterministic, reproducible
       │    fragment = TaskFragment(task_uid, parent_job_uid=ROAR_DRIVER_JOB_UID)
       ├─ All open() and boto3 calls go to current fragment
       ├─ Detects task end: task_id changes or process flush
       └─ Finalises fragment:
            - hash each written file with blake3 at close() time
            - emit fragment to RoarLogCollectorActor
```

### Task UID derivation

Ray's `task_id` is a deterministic Ray-internal ID. We derive `job_uid` from it:

```python
import hashlib

def task_uid(ray_task_id: str, job_id: str) -> str:
    """Deterministic, reproducible job_uid for a Ray task."""
    h = hashlib.blake3(f"{job_id}:{ray_task_id}".encode()).hexdigest()
    return h[:8]
```

**Yes, each worker task gets a unique job ID.** It's derived from the Ray task ID and the
roar job ID, so it's both unique and deterministic — the same task in a re-run produces the
same `job_uid`, enabling GLaaS dedup via `step_identity`.

---

## Fragment Schema

Each task produces a `TaskFragment` — a self-contained lineage record:

```python
@dataclass
class TaskFragment:
    job_uid: str                  # derived from ray task_id
    parent_job_uid: str           # driver's job_uid
    ray_task_id: str              # raw Ray task ID
    ray_worker_id: str
    ray_node_id: str
    started_at: float
    ended_at: float
    command: str                  # "ray_task:{function_name}"
    reads: list[ArtifactRef]      # path + hash + size
    writes: list[ArtifactRef]     # path + hash + size
    packages: dict[str, str]      # {name: version}, first task only
    exit_code: int                # 0 unless task raised
```

```python
@dataclass
class ArtifactRef:
    path: str
    hash: str | None      # blake3 for local files; etag for S3
    hash_algorithm: str   # "blake3" | "etag"
    size: int
    capture_method: str   # "python" | "proxy" | "preload"
```

Fragments are sent to `RoarLogCollectorActor.append_fragment(fragment)` as msgpack dicts.

---

## Driver-Side Collector: Fragment → DB

When `ray.shutdown()` is called (patched by `sitecustomize.py`), the driver runs the
collector. The collector now has a richer job to do:

```python
def collect(project_dir, log_dir, driver_job_uid):
    # 1. Pull all fragments from actor
    fragments = actor.get_all_fragments()

    # 2. For each fragment, write to local DB:
    for frag in fragments:
        # 2a. Upsert artifacts (content-addressed — same hash = same row)
        for ref in frag.reads + frag.writes:
            upsert_artifact(ref.path, ref.hash, ref.hash_algorithm, ref.size)

        # 2b. Insert task job record
        insert_job(
            job_uid=frag.job_uid,
            parent_job_uid=frag.parent_job_uid,
            command=frag.command,
            job_type="ray_task",
            timestamp=frag.started_at,
            duration_seconds=frag.ended_at - frag.started_at,
            exit_code=frag.exit_code,
            session_id=<driver session id>,
            step_number=<same as driver step>,
        )

        # 2c. Record lineage edges
        for ref in frag.reads:
            insert_job_input(frag.job_uid, artifact_id(ref.hash), ref.path)
        for ref in frag.writes:
            insert_job_output(frag.job_uid, artifact_id(ref.hash), ref.path)

    # 3. Link driver job's inputs to task outputs (driver consumed worker outputs)
    #    - already handled by driver's own sitecustomize tracking (open() on checkpoint files)

    # 4. Compute composite job hash: blake3(sorted(write_hashes across all tasks))
    #    → store on driver job as composite output artifact
```

### Deduplication

Two workers that read the same dataset shard produce the same blake3 hash → same artifact
row. `INSERT OR IGNORE` on `artifact_hashes(algorithm, digest)` handles this naturally.
Two workers writing the same output (e.g., same config file) also deduplicate.

---

## GLaaS Registration: What Gets Sent

When the user runs `roar register model.pt`, the `LineageCollector` now walks a richer graph.

### Registration payload for a distributed job

```
Session: { hash, git_repo, git_commit, git_branch }

Artifacts (batch):
  - shard_0.parquet  { hash: H_s0, size, source_type: "s3" }
  - shard_1.parquet  { hash: H_s1, size, source_type: "s3" }
  - checkpoint_0.pt  { hash: H_c0, size }
  - checkpoint_1.pt  { hash: H_c1, size }
  - final_model.pt   { hash: H_fm, size }

Jobs (in order of timestamp):
  1. Job "python train.py"            job_uid: a1b2c3d4  step: 1  job_type: null
  2. Job "ray_task:train_shard"       job_uid: t001xxxx  step: 1  job_type: ray_task  parent: a1b2c3d4
  3. Job "ray_task:train_shard"       job_uid: t002xxxx  step: 1  job_type: ray_task  parent: a1b2c3d4

Job inputs/outputs:
  a1b2c3d4 inputs:  [H_s0, H_s1, H_c0, H_c1]   (driver reads shards + checkpoints)
  a1b2c3d4 outputs: [H_fm]                       (driver writes final model)
  t001xxxx inputs:  [H_s0]                       (task 1 reads shard 0)
  t001xxxx outputs: [H_c0]                       (task 1 writes checkpoint 0)
  t002xxxx inputs:  [H_s1]
  t002xxxx outputs: [H_c1]
```

### DAG reconstruction

Given `final_model.pt` (hash H_fm):

```
GET /api/v1/artifacts/H_fm/dag

Response:
  artifact: { hash: H_fm, path: "final_model.pt" }
  jobs: [
    { uid: a1b2c3d4, command: "python train.py", children: [
        { uid: t001xxxx, command: "ray_task:train_shard",
          inputs: [H_s0], outputs: [H_c0] },
        { uid: t002xxxx, command: "ray_task:train_shard",
          inputs: [H_s1], outputs: [H_c1] }
    ]},
  ]
  inputs_of_root: [H_s0, H_s1]     ← shards needed to reproduce
  external_deps: [H_s0, H_s1]      ← if shards came from S3
```

The response can be rendered as:

```
shard_0.parquet ──┐
                  ├──► [ray_task:train_shard / t001xxxx] ──► checkpoint_0.pt ──┐
shard_1.parquet ──┤                                                              ├──► [python train.py] ──► final_model.pt
                  └──► [ray_task:train_shard / t002xxxx] ──► checkpoint_1.pt ──┘
```

**This is the total lineage.** Every artifact is content-addressed, every task is a
traceable job, and the graph can be reproduced on another machine given the input shards
and the git commit.

---

## step_identity for Ray Tasks

`step_identity` = `hash(command, sorted(input_hashes))` is used for dedup — same inputs +
same command = same task, even across runs.

For Ray tasks:

```python
def ray_task_step_identity(function_name: str, input_hashes: list[str]) -> str:
    identity = f"ray_task:{function_name}:" + ":".join(sorted(input_hashes))
    return blake3(identity.encode()).hexdigest()[:16]
```

If you run the same training job twice on the same data, the task job_uids are identical
(derived from task_id which is deterministic given the same Ray job), and the step_identities
match → GLaaS deduplicates and the second run's registration is a no-op.

---

## Implementation Plan

### Phase 1: Schema & fragment model (no user-visible changes)

1. `roar/db/schema.py` — add `parent_job_uid` to jobs table
2. `roar/ray/fragment.py` — `TaskFragment` and `ArtifactRef` dataclasses
3. `roar/ray/actor.py` — add `append_fragment()` and `get_all_fragments()` methods
4. `roar/ray/collector.py` — rewrite to process fragments, write task jobs to DB
5. Unit tests for fragment → DB merge, dedup, parent linkage

### Phase 2: roar-worker entry point

1. `roar/ray/roar_worker.py` — main entry point: process startup, task boundary detection,
   fragment lifecycle, streaming blake3 hash-on-close
2. `pyproject.toml` — add `roar-worker = "roar.ray.roar_worker:main"` entry point
3. `sitecustomize.py` — update `_prepare_worker_runtime_env` to set
   `py_executable = "roar-worker"` and pass `ROAR_DRIVER_JOB_UID`
4. Unit tests (TDD): task boundary detection, fragment emission, hash-on-close
5. E2E test: multi-task Ray job produces per-task job records in DB

### Phase 3: GLaaS API extension

1. Add `parent_job_uid` to `RegisterJobRequest` in `roar/core/models/glaas.py`
2. Update `GlaasClient.register_job` and `register_jobs_batch` to include `parent_job_uid`
3. Update `RegistrationCoordinator` to send task jobs with correct parent linkage
4. Update `LineageCollector` to include `ray_task` type jobs in lineage walks
5. Server-side: `parent_job_uid` stored and surfaced in DAG responses
   (coordinate with GLaaS API changes — this is the one place that requires server work)

### Phase 4: roar reproduce support

1. `GET /api/v1/artifacts/{hash}/dag` returns nested children
2. `roar reproduce` replays tasks in topological order respecting parent-child structure
3. For Ray tasks: reproduce driver job (which re-runs Ray, which re-runs workers)

---

## Open Questions for Trevor

1. **GLaaS server timeline**: `parent_job_uid` in the server DAG response is needed for full
   visualisation. Is that a near-term GLaaS change or should we design the local side first
   and treat GLaaS as a flat list of jobs for now?

2. **Actor task attribution**: Ray actor *methods* (long-lived stateful objects) don't have a
   clean start/end per method call the way remote functions do. Should actor method calls be
   grouped into a single long-lived "actor job", or attributed per-call? The per-call model
   is more granular but harder to detect boundaries for.

3. **task_id stability**: Ray's `task_id` changes between runs even for the same logical
   task. This means `job_uid` (derived from task_id) will differ between runs. That's fine
   for lineage recording, but it means `step_identity` (not `job_uid`) is the dedup key for
   "same logical task, same inputs". Is that acceptable, or do we need stable task IDs?

4. **S3 hash consistency**: Worker S3 events have ETags (MD5). The rest of the lineage uses
   blake3. Two options:
   - Keep ETags for S3, document the inconsistency
   - Route S3 ops through the proxy in workers and compute blake3 there (requires proxy
     running on each node or routing all S3 through the driver proxy)
   Which matters more: uniformity or implementation simplicity?

5. **Worker package provenance**: Currently `roar run` captures the full package list of the
   driver environment. Workers may have a different environment (different `runtime_env.pip`).
   Should `roar-worker` capture the worker's package list per fragment (first task only) and
   include it in the job metadata? Or is driver-only sufficient?

6. **Intermediate artifacts**: Should checkpoint_0.pt and checkpoint_1.pt be registered with
   GLaaS? They're intermediate artifacts — useful for debugging but noisy for most searches.
   Suggest: register them locally always, register to GLaaS only if `roar register --deep` or
   if they're explicitly referenced by a downstream job outside the current session.

---

## Summary

| Concept | Current (non-Ray) | Proposed (Option C) |
|---|---|---|
| Jobs per `roar run` | 1 | 1 driver + N task jobs |
| Task attribution | None | Per-task via `job_uid` |
| Content hash (local) | blake3 | blake3 (hash-on-close in worker) |
| Content hash (S3) | ETag | ETag (blake3 via proxy: future) |
| Parent linkage | N/A | `parent_job_uid` |
| Dedup key | `step_identity` | `step_identity` (same algorithm) |
| GLaaS registration | Flat job list | Nested with parent_job_uid |
| DAG reconstruction | Linear per session | Distributed fan-out/fan-in |
| Reproduce support | Full | Full (replay driver = replays workers) |
