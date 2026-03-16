# Rust Artifact Hashing Acceleration Proposal

## Summary
This proposal moves artifact hashing from Python into a shared Rust library and exposes it to `roar-cli` via an in-process Python extension.

Primary outcomes:
1. lower wall-clock time for artifact hashing
2. one hashing implementation used consistently across `run`, `get`, `put`, `register`, and `lineage`
3. preserved hash semantics for GLaaS API compatibility
4. easier future extension for new algorithms and backends

## Current State Review
Artifact hashing is implemented in multiple places today:
1. central service:
- `roar/db/services/hashing.py`
- supports cache + multi-hash single pass
2. duplicate direct hashing paths:
- `roar/services/get/service.py`
- `roar/services/put/service.py`
- `roar/services/registration/register_service.py`
- `roar/cli/commands/lineage.py`
3. algorithm config/validation:
- `roar/integrations/config/access.py` (`blake3`, `sha256`, `sha512`, `md5`)

Observed issues:
1. duplicated hashing logic and chunking behavior
2. inconsistent fallback behavior (`blake3` fallback to `sha256` in some paths)
3. unnecessary Python-level hashing overhead for high-file-count workloads
4. direct hashing call sites bypass the central cache-aware service

## Goals
1. move file hashing execution into Rust
2. call Rust hashing from Python without subprocess overhead
3. keep output exactly compatible with existing artifact records and GLaaS payloads
4. centralize artifact hashing usage through one Python service interface
5. remove deprecated Python hashing code paths

## Non-Goals
1. changing artifact schema in DB or GLaaS
2. changing default hash policy (`blake3` primary)
3. replacing non-artifact security/auth hashing (signatures, fingerprints)

## Options Considered

### Option 1: Rust Hashing Binary + Subprocess Calls
Python shells out to a Rust executable for hashing.

Pros:
1. minimal Python packaging changes
2. easy local debugging via CLI

Cons:
1. process startup and IPC overhead on each call
2. weaker gains for many small files
3. awkward integration with cache-aware multi-file workflows

### Option 2 (Recommended): In-Process Rust Extension via PyO3
Add a Rust crate for hashing core logic and a PyO3 binding module imported directly by Python.

Pros:
1. best runtime performance (no process boundary)
2. can release GIL during hashing
3. supports fast batch and multi-algorithm hashing APIs
4. clean reuse of Rust logic in other crates later

Cons:
1. requires native wheel build updates in CI/publish workflows
2. requires careful packaging across platforms

### Option 3: Rust Hashing Daemon
Long-lived daemon with RPC from Python.

Pros:
1. high throughput for very large queues

Cons:
1. highest operational complexity
2. unnecessary for current CLI architecture

## Recommended Architecture

### Rust Workspace Additions
Add two crates under `rust/crates/`:
1. `artifact-hash-core`
- pure Rust hashing library
- APIs:
  - `hash_file(path, algorithms) -> map`
  - `hash_files(paths, algorithms, parallelism) -> map`
- one-pass multi-hash per file
- deterministic lowercase hex output
2. `artifact-hash-py`
- PyO3 `cdylib` exposing Python module (e.g. `roar._hash_native`)
- thin binding over `artifact-hash-core`
- GIL released while hashing

### Python Integration
1. add `RustHashingService` implementing existing `HashingService` protocol
2. make `DefaultHashingService` delegate hasher creation/execution to native module
3. route duplicate hashing call sites through shared hashing service:
- `roar/services/get/service.py`
- `roar/services/put/service.py`
- `roar/services/registration/register_service.py`
- `roar/cli/commands/lineage.py`
4. remove Python strategy/registry code once no call sites remain

### API Surface
Expose these binding functions:
1. `compute_hash(path: str, algorithm: str) -> str`
2. `compute_hashes(path: str, algorithms: list[str]) -> dict[str, str]`
3. `compute_hashes_batch(paths: list[str], algorithms: list[str], workers: int | None) -> dict[str, dict[str, str]]`

Design notes:
1. preserve lowercase digests and algorithm names exactly as today
2. return deterministic algorithm order to match existing expectations
3. keep file-open and IO errors mapped to Python exceptions/`None` semantics currently used by services

## GLaaS Compatibility Requirements
1. preserve hash algorithm labels exactly: `blake3`, `sha256`, `sha512`, `md5`
2. preserve digest encoding: lowercase hex, no prefixes
3. preserve artifact registration payload shape (`hashes: [{algorithm, digest}]`)
4. keep `blake3` as primary unless user overrides config

## Migration Plan

### Phase 1: Baseline and Parity Harness
1. add deterministic parity tests comparing current Python hashing outputs vs Rust candidate outputs
2. add benchmark harness for representative workloads:
- few large files
- many small files
- mixed read/write pipeline artifacts
3. freeze expected output fixtures for GLaaS payloads

### Phase 2: Implement Rust Core
1. create `artifact-hash-core` with supported algorithms
2. implement single-file and batch APIs
3. add Rust unit tests for:
- all algorithms
- empty files
- large files
- invalid paths

### Phase 3: Add PyO3 Binding
1. create `artifact-hash-py` crate and module import path `roar._hash_native`
2. map Rust errors to Python exceptions cleanly
3. add Python unit tests for binding behavior and parity

### Phase 4: Python Service Refactor
1. implement `RustHashingService`
2. migrate `DefaultHashingService` to use native backend
3. replace direct hashing in:
- `get` service
- `put` service
- `register` service
- `lineage` command helper
4. remove deprecated duplicated hashing functions

### Phase 5: Remove Legacy Hashing Paths
1. delete Python hash strategy/registry modules after migration
2. remove direct `blake3` imports where service integration replaces them
3. keep only one artifact hashing code path

## Test Plan
1. parity tests:
- native vs legacy digest equivalence for all algorithms
2. behavior tests:
- cache hit/miss behavior unchanged in DB hash cache repo
- `get` hash verification path unchanged
- `register` and `lineage` path lookup by hash unchanged
3. integration tests:
- `roar run --hash sha256`
- `roar get --hash ...`
- `roar put` lineage registration payload
4. performance tests:
- benchmark target: measurable improvement over baseline in both single large file and many small file scenarios

## GitHub Workflow and Deploy Implications

### CI (`.github/workflows/ci.yml`)
1. add native extension build step from workspace before Python tests
2. cache Rust dependencies and target dir for extension crates
3. run parity test suite in CI to block digest drift

### Publish (`publish-pypi.yml`, `publish-testpypi.yml`)
1. include native extension artifacts in wheel builds
2. ensure platform-tagged wheels are built for supported runner matrix
3. keep tracer/proxy binary copy steps unchanged except for workspace path consistency

### Packaging
1. keep current project layout, but add native extension build integration (PyO3-compatible tooling)
2. ensure local `pip install -e .` builds extension or fails with explicit error
3. avoid fallback silent behavior that can mask performance regressions

## Risks and Mitigations
1. Risk: digest drift from current implementation.
- Mitigation: strict parity tests against known vectors + legacy implementation during migration.
2. Risk: packaging complexity across OS/arch.
- Mitigation: explicit wheel matrix in publish workflows and CI smoke-install checks.
3. Risk: mixed code paths leave duplication.
- Mitigation: remove direct hashing methods after service migration and enforce one interface.
4. Risk: subtle behavior changes on missing files/permissions.
- Mitigation: preserve `None`/error semantics in service layer contract tests.

## Acceptance Criteria
1. artifact hashing for `run/get/put/register/lineage` is executed by Rust code via in-process Python calls
2. no duplicate direct hashing implementations remain in service/CLI paths
3. digest outputs remain GLaaS-compatible with parity tests passing
4. CI and publish workflows produce installable artifacts containing the native hashing extension
5. benchmark report shows clear improvement vs current Python baseline

## Decision Log
1. Option selection:
- Decision: proceed with Option 2 (in-process PyO3 extension).
- Rationale: best performance and cleanest long-term architecture for shared hashing logic.
2. Compatibility boundary:
- Decision: artifact hashing behavior remains wire-compatible with existing GLaaS expectations.
- Rationale: avoid API/schema risk while improving execution speed.
3. Duplication policy:
- Decision: remove duplicate direct hashing functions after migration, not maintain dual implementations.
- Rationale: one code path reduces drift and maintenance cost.

## Implementation Notes (2026-02-11)
1. Landed native hashing crates:
- `rust/crates/artifact-hash-core`
- `rust/crates/artifact-hash-py`
2. Landed Python backend + call site migration:
- `roar/db/hashing/backend.py`
- `roar/db/services/hashing.py`
- `roar/services/get/service.py`
- `roar/services/put/service.py`
- `roar/services/registration/register_service.py`
- `roar/cli/commands/lineage.py`
3. Removed deprecated hashing strategy/registry code:
- `roar/db/hashing/registry.py`
- `roar/db/hashing/strategies.py`
4. Benchmark harness added:
- `tests/benchmarks/bench_artifact_hashing.py`
5. Completed batch-first hashing migration across call sites:
- `roar/db/services/job_recording.py` now pre-hashes all input/output paths in one batch.
- `roar/services/get/service.py` now hashes prefix downloads in one batch.
- `roar/services/put/service.py` now hashes resolved sources in one batch.
- `roar/services/registration/register_service.py` and `roar/cli/commands/lineage.py` now use the same batch backend API for singleton lookups.
6. Landed small-file performance optimizations:
- `rust/crates/artifact-hash-core/src/lib.rs` now uses adaptive chunk sizing and reuses a scratch buffer in sequential batch mode.
- `roar/db/hashing/backend.py` now avoids native-result remapping and redundant path normalization in the native path.
7. Added native worker auto-selection when `workers` is not explicitly provided:
- `roar/db/hashing/backend.py` now derives worker count from file count, sample file sizes, and CPU count.
- Explicit `workers` continues to override the heuristic.
8. Workflow hardening for native hashing distribution:
- `.github/workflows/ci.yml` now builds and import-smoke-tests `roar._hash_native` in the test matrix.
- `.github/workflows/publish-pypi.yml` and `.github/workflows/publish-testpypi.yml` now build `roar._hash_native` and verify wheels contain both tracer binaries and the native hashing module.
- `scripts/build_hash_native.sh` now supports CI/global Python while still preferring `.venv` locally.
9. Build backend consolidation to maturin:
- Root `pyproject.toml` now uses `maturin` as the package build backend (`setuptools` backend removed).
- Root `[tool.maturin]` now points at `rust/crates/artifact-hash-py/Cargo.toml` for wheel/sdist builds.
- `setup.py` was removed to eliminate deprecated dual build paths.
- Publish workflows now build wheel/sdist with `maturin build`/`maturin sdist`.
10. Linux wheel matrix for release packaging:
- Publish workflows now build one Linux wheel per supported Python version (`cp310`, `cp311`, `cp312`, `cp313`) and aggregate them before upload.
- Rust tracer/proxy binaries are built once and reused across wheel jobs via workflow artifacts.
- Release gating now verifies all expected wheel tags are present before upload.

### Benchmark Snapshot
Run date: 2026-02-11

1. Mixed dataset (`608` files, `68.7 MiB`, `blake3+sha256`):
- Before batch-all refactor:
  - Python baseline mean: `0.058s`
  - Native mean: `0.111s`
  - Relative speed: `0.52x` (native slower)
- After batch-all refactor (pre-optimization):
  - Python baseline mean: `0.056s`
  - Native mean: `0.113s`
  - Relative speed: `0.50x` (native slower)
- After small-file optimization:
  - Python baseline mean: `0.055s`
  - Native mean: `0.052s`
  - Relative speed: `1.07x` (native faster)
- After auto worker selection:
  - Python baseline mean: `0.057s` (`tests/benchmarks/results/hash_bench_latest.json`)
  - Native mean: `0.018s` (`tests/benchmarks/results/hash_bench_latest.json`)
  - Relative speed: `3.11x` (native faster)
2. Large-file dataset (`4` files, `256 MiB`, `blake3+sha256`):
- Before batch-all refactor:
  - Python baseline mean: `0.215s`
  - Native mean: `0.182s`
  - Relative speed: `1.18x` (native faster)
- After batch-all refactor (pre-optimization):
  - Python baseline mean: `0.215s`
  - Native mean: `0.179s`
  - Relative speed: `1.20x` (native faster)
- After small-file optimization:
  - Python baseline mean: `0.208s`
  - Native mean: `0.177s`
  - Relative speed: `1.17x` (native faster)
- After auto worker selection:
  - Python baseline mean: `0.210s` (`tests/benchmarks/results/hash_bench_large.json`)
  - Native mean: `0.180s` (`tests/benchmarks/results/hash_bench_large.json`)
  - Relative speed: `1.17x` (native faster)
3. Mixed dataset with native workers (`workers=8`):
- Before small-file optimization:
  - Python baseline mean: `0.057s`
  - Native mean: `0.233s`
  - Relative speed: `0.24x` (native slower)
- After small-file optimization:
  - Python baseline mean: `0.055s`
  - Native mean: `0.017s`
  - Relative speed: `3.16x` (native faster)
- With explicit `workers=8` after auto-selection landed:
  - Python baseline mean: `0.056s` (`tests/benchmarks/results/hash_bench_latest_workers8.json`)
  - Native mean: `0.018s` (`tests/benchmarks/results/hash_bench_latest_workers8.json`)
  - Relative speed: `3.14x` (native faster)

### Decision Update
1. Keep the native backend in place with Python fallback and parity checks.
2. Keep batch orchestration as the standard call pattern for all artifact hashing paths.
3. Native hashing is now faster on both mixed (`3.11x`) and large-file (`1.17x`) datasets with default settings after enabling auto worker selection.
4. Auto worker selection is the default behavior when `workers` is omitted; explicit worker configuration remains opt-in override.
5. Next optimization step should focus on refining auto-selection thresholds using real project telemetry and hardware variability testing.
