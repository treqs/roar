# Ray Distributed Execution for `roar run`

## Goal

Enable `roar run` and `roar build` to execute distributed workloads on Ray while
preserving lineage fidelity so existing DAG views, `roar put`, and `roar register`
continue to work with GLaaS.

## Implemented Design

### 1) Execution backend split at coordinator level

- `RunCoordinator` now branches by `RunContext.execution_backend`:
  - `local`: existing tracer-based flow.
  - `ray`: distributed flow with direct command execution + Ray event ingestion.

### 2) Ray lineage capture contract

- New runtime helpers in `roar.ray`:
  - `@traced_remote`
  - `record_input(path)` / `record_output(path)`
  - `record_input_ref(ref)` / `record_output_ref(ref)`
- A detached Ray actor aggregates task events for a single run.
- Run-scoped env vars are injected by `RunCoordinator`:
  - `ROAR_DISTRIBUTED_BACKEND=ray`
  - `ROAR_RAY_LINEAGE_ACTOR`
  - `ROAR_RAY_NAMESPACE`
  - `ROAR_RAY_ADDRESS`
  - `ROAR_RAY_RUN_ID`

### 3) Event ingestion into existing DB graph model

- New `RayLineageRecorder` ingests task events as regular roar jobs/artifacts.
- Logical Ray object refs are stored as synthetic artifacts:
  - path format: `ray://object/<ref>`
  - hashes: deterministic (`blake3`, `sha256`)
- This preserves job-to-job dependencies through artifact edges, so existing
  lineage traversal and GLaaS registration continue to function.

### 4) Fallback behavior

- If a Ray run completes without task events, a fallback driver job is recorded
  so the run is still represented in session history.
- If Ray package is missing and backend is `ray`, command fails fast with a
  clear error.

### 5) Config + CLI semantics

- Config keys:
  - `execution.backend` (`local` or `ray`)
  - `execution.ray_address`
  - `execution.ray_namespace`
- CLI flags:
  - `--backend`
  - `--ray-address`
  - `--ray-namespace`

## Testing Strategy

### Unit coverage

- CLI forwarding tests for execution backend flags.
- Config tests for execution backend defaults and validation.
- Coordinator tests for Ray branch orchestration.
- `RayLineageRecorder` tests for:
  - task DAG persistence with ref edges
  - fallback job creation when no events are emitted

### Docker-compose E2E coverage

- Added `tests/e2e/ray_cluster/docker-compose.yml`:
  - 1 Ray head
  - 2 Ray workers
- Added `tests/e2e/test_ray_distributed_e2e.py`:
  - Initializes a temp git repo + roar project.
  - Runs a distributed workflow using `@traced_remote`.
  - Verifies recorded lineage and DAG traversability in local DB.
- Test runs only when `ROAR_RUN_DOCKER_E2E=1`.

## CI implications

- Added `test-ray-distributed-e2e` job in CI:
  - installs `ray`
  - runs e2e test with Docker Compose
- This gives regression coverage for distributed lineage ingestion without
  altering existing non-e2e test lanes.

## Known constraints

- `roar.ray` instrumentation is currently explicit (`@traced_remote` + record
  helpers). Automatic capture for arbitrary Ray code is out of scope.
- Proxy traffic from remote worker containers is not guaranteed to route through
  a local loopback proxy without extra network plumbing.
