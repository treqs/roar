# Rust Tracer Workspace Consolidation Proposal

## Summary
This proposal consolidates the Rust tracer code into a single workspace directory, extracts shared tracer logic into reusable crates, and introduces a predictable build surface for CI and publishing.

The target outcome is:
1. one Rust home for tracer code
2. one lockfile and workspace dependency graph
3. shared report/process/path/fd logic across tracer implementations
4. lower effort to add a new tracer backend in the future

## Current State Review
The current tracer Rust code is split across two roots:
1. `tracer/` (ptrace tracer; monolithic `src/main.rs`, ~859 LOC)
2. `tracer-ebpf/` (workspace with `userspace`, `ebpf`, `common`)

Key issues observed:
1. Workspace fragmentation:
- separate Cargo roots and lockfiles (`tracer/Cargo.lock` and `tracer-ebpf/Cargo.lock`)
- separate `target/` trees and path assumptions throughout tests/workflows
2. Duplicate tracer logic:
- process metadata capture from `/proc` appears in both ptrace and eBPF userspace
- relative-path resolution appears in both implementations
- report schema structs overlap conceptually but diverge in type definitions
3. Duplicate eBPF userspace wiring:
- tracepoint attach helpers and program attach lists are duplicated between `userspace/src/main.rs` and `userspace/src/daemon.rs`
4. Build/deploy drift risk:
- CI and publish workflows hardcode different manifest paths and artifact locations
- legacy `setup.py` fallback path assumptions can drift from workspace builds

## Goals
1. Move tracer-related Rust code under one top-level directory.
2. Define common crates for shared tracer behavior and output schema.
3. Keep binary names stable:
- `roar-tracer`
- `roar-tracer-ebpf`
- `roard`
4. Preserve current runtime behavior and GLaaS compatibility.
5. Make backend addition procedural instead of ad-hoc.

## Non-Goals
1. Rewriting the eBPF probe architecture.
2. Changing the on-disk MessagePack contract in a breaking way.
3. Broad runtime policy changes in Python tracer selection.

## Options Considered

### Option A: Directory Move Only
Move folders under one parent without code extraction.

Pros:
1. lowest immediate migration risk
2. fast path updates in CI

Cons:
1. duplication remains
2. new backend work remains expensive
3. technical debt survives mostly intact

### Option B (Recommended): Workspace + Shared Crates
Move into one Rust workspace and extract shared crates used by ptrace and eBPF userspace.

Pros:
1. real DRY improvement in tracer internals
2. clearer backend extension model
3. single dependency/lockfile management

Cons:
1. moderate refactor size
2. requires staged rollout and targeted regression tests

## Recommended Architecture

### Directory Layout
```text
rust/
  Cargo.toml
  Cargo.lock
  crates/
    tracer-schema/
    tracer-runtime/
    tracer-fd/
  services/
    proxy/
      Cargo.toml
      src/
  tracers/
    ptrace/
      Cargo.toml
      src/main.rs
    ebpf/
      common/
      probe/
      userspace/
```

Notes:
1. Rename `tracer-ebpf/ebpf` to `tracers/ebpf/probe` for clearer intent.
2. Keep existing binary names unchanged from Cargo `[[bin]]`.
3. Keep eBPF `rust-toolchain.toml` scoped to `tracers/ebpf/probe` (nightly only where needed).

### Shared Crates

#### `tracer-schema`
Single source of truth for serialized report and process/file types.

Includes:
1. `TracerReport`
2. `ProcessInfo`
3. `FileRecord`
4. optional fields (`events_dropped`, `chunk_size`, chunk arrays)

#### `tracer-runtime`
Runtime helpers shared by ptrace and eBPF userspace.

Includes:
1. timestamp helper
2. `/proc` process capture utilities
3. path resolution utilities
4. child spawn/stop/resume/wait helpers

#### `tracer-fd`
Common fd/path event aggregation primitives.

Includes:
1. fd open/close/dup/clone tracking
2. read/write/pread/pwrite tracking
3. optional chunk tracking
4. report builder into `tracer-schema`

### eBPF Userspace DRY Extraction
Extract shared attach logic from `userspace/src/main.rs` and `userspace/src/daemon.rs`:
1. `userspace/src/attach.rs`:
- `attach_tp(...)`
- `attach_all_tracepoints(...)`
- optional `load_bpf_object(...)`
2. keep daemon-specific and inline-specific lifecycle code separate

## Adding New Tracers (Future Model)
Each new backend should follow one pattern:
1. add crate under `rust/tracers/<backend>/`
2. depend on `tracer-schema` and `tracer-runtime` (and `tracer-fd` if fd-based)
3. emit `TracerReport` from shared schema
4. expose binary `roar-tracer-<backend>` (or keep a stable alias if needed)
5. register package in workspace `members`

This keeps backend work focused on event collection, not report/plumbing duplication.

## Migration Plan

### Phase 1: Workspace Consolidation (No Behavior Change)
1. Create `rust/Cargo.toml` workspace.
2. Move:
- `tracer/` -> `rust/tracers/ptrace/`
- `tracer-ebpf/common` -> `rust/tracers/ebpf/common/`
- `tracer-ebpf/ebpf` -> `rust/tracers/ebpf/probe/`
- `tracer-ebpf/userspace` -> `rust/tracers/ebpf/userspace/`
- `proxy/` -> `rust/services/proxy/`
3. Remove old Cargo roots and lockfiles after build parity verification.
4. Keep binary names and CLI behavior unchanged.

### Phase 2: Shared Crate Introduction
1. Add `tracer-schema` and migrate ptrace + eBPF report types.
2. Add `tracer-runtime` and migrate process/path/time helpers.
3. Add `tracer-fd` and incrementally migrate fd/event aggregation.

### Phase 3: eBPF Userspace DRY Cleanup
1. Extract attach/load helpers shared by inline and daemon modes.
2. Remove duplicated tracepoint attach lists.
3. Keep existing tests for daemon lifecycle and auto-spawn behavior.

### Phase 4: Python + Docs + Path Cleanup
1. Update Python tracer discovery paths in:
- `roar/services/execution/tracer.py`
- `roar/cli/commands/tracer.py`
2. Update build hints shown to users.
3. Update test fixtures that hardcode old target paths (`tests/ebpf`, benchmarks).
4. Update deployment docs and developer docs.

## GitHub Workflow Implications

### CI (`.github/workflows/ci.yml`)
Current workflow has separate hardcoded jobs for `tracer/` and `tracer-ebpf/`.

Proposed update:
1. replace hardcoded manifest paths with workspace package builds:
- `cargo build --release --manifest-path rust/Cargo.toml -p roar-tracer`
- `cargo build --release --manifest-path rust/Cargo.toml -p roar-tracer-ebpf`
- `cargo build --release --manifest-path rust/Cargo.toml -p roar-proxy`
2. keep nightly + `rust-src` setup only for eBPF build path
3. standardize artifact paths to `rust/target/release/*`
4. add Rust cache step for workspace (example: `Swatinem/rust-cache`)

Result:
1. less workflow duplication
2. consistent artifact locations
3. easier backend expansion by adding package names to one matrix/list

### Publish Workflows (`publish-pypi.yml`, `publish-testpypi.yml`)
Current publish jobs should build all Rust binaries explicitly from workspace packages and package with a single maturin backend.

Proposed update:
1. build proxy + tracer binaries explicitly from workspace packages before Python wheel build
2. copy binaries from `rust/target/release/` to `roar/bin/`
3. build wheel/sdist through root maturin config (no setuptools fallback)
4. avoid hidden coupling to old path assumptions

Result:
1. deterministic packaging
2. fewer path regressions during refactor
3. clearer release troubleshooting

### Local Dev Build/Deploy Process
Add one script as the canonical entrypoint:
1. `scripts/build_rust_tracers.sh`

Script responsibilities:
1. build ptrace and eBPF userspace binaries from workspace
2. handle nightly/bpf-linker checks for eBPF
3. copy produced binaries to `roar/bin/` when requested

Then use this script in:
1. local docs
2. CI workflows
3. publish workflows
4. no setuptools fallback path

## Risk Assessment and Mitigations
1. Risk: path breakage in Python tracer binary discovery.
- Mitigation: update path probes to include new `rust/target/release` first, keep `roar/bin` fallback.
2. Risk: eBPF build break due to probe path/toolchain assumptions.
- Mitigation: preserve scoped nightly toolchain for probe crate and validate build on CI before cutover.
3. Risk: report schema drift between ptrace and eBPF.
- Mitigation: shared `tracer-schema` crate plus snapshot tests for serialized output keys.
4. Risk: workflow regressions.
- Mitigation: introduce workspace path changes in one PR with CI green gate before shared-logic refactor.

## Acceptance Criteria
1. All tracer Rust code is under `rust/`.
2. Proxy and tracer binaries compile from one workspace root.
3. `roar run` works with ptrace and eBPF unchanged from user perspective.
4. eBPF daemon and inline paths still pass existing tests.
5. CI and publish workflows use workspace paths and produce expected binaries.
6. Adding a new backend requires no workflow path surgery, only package registration.

## Decision Log
1. Option selection:
- Decision: proceed with Option B (workspace + shared crates).
- Rationale: this directly addresses maintainability, extension cost, and duplicated logic.
2. Compatibility policy:
- Decision: no backward-compatibility shims for old Rust directory paths.
- Rationale: clean cutover lowers long-term complexity and avoids dual-path maintenance.
3. Proxy placement:
- Decision: move the S3 proxy crate into `rust/services/proxy` and build it from the shared workspace.
- Rationale: one Rust root avoids path drift across CI/publish/runtime discovery and keeps build behavior consistent.
4. `tracer-fd` scope:
- Decision: land chunk-aware fd/path aggregation in `tracer-fd` now and use it from both ptrace and eBPF userspace.
- Rationale: this removes duplicate state machines immediately and aligns lineage semantics across tracer backends.
5. Python build backend:
- Decision: move fully to root `maturin` backend and remove `setup.py`.
- Rationale: one build path is easier to reason about, avoids hidden fallback behavior, and aligns Rust extension packaging with release workflows.
