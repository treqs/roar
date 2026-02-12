# Tracer CLI Redesign and Backend Alignment Proposal

## Summary
This proposal redesigns tracer-related CLI behavior, unifies ptrace/eBPF tracer output contracts, hardens proxy lifecycle handling, and expands tests so `roar run` and `roar build` reliably produce lineage across tracer backends without breaking GLaaS registration semantics.

The key design principle is:
- backend selection should be explicit and semantically clear to users
- backend execution should be resilient by default (`auto` with safe fallback)
- provenance ingestion should accept both tracer formats and normalize to one internal model
- proxy behavior should be deterministic and leak-free across success/failure paths

## Current Issues
1. eBPF and ptrace produce different MessagePack shapes, but provenance loading expects ptrace fields only.
2. `tracer.mode=auto` currently means "prefer eBPF if binary exists", not "prefer eBPF if actually usable".
3. Runtime fallback behavior is not robust when eBPF fails at execution time.
4. Proxy teardown is not guaranteed on all early-return failure paths.
5. Tracer configuration UX is split between `roar config tracer ...` and implicit behavior in runtime code, which is hard to reason about.
6. Test coverage is thin around tracer config UX, fallback semantics, and provenance contract compatibility between tracers.

## Goals
1. Provide a clear tracer CLI UX for:
- setting a default tracer policy
- checking backend readiness
- configuring backend-specific requirements (eBPF setup)
2. Ensure both ptrace and eBPF can drive `roar run`/`roar build` lineage end-to-end.
3. Ensure proxy behavior remains correct with both tracer backends and during fallback.
4. Keep GLaaS API payload compatibility unchanged.
5. Add tests that lock in behavior and prevent regressions.

## Non-Goals
1. Introducing a third backend (for example `LD_PRELOAD`) in this iteration.
2. Changing GLaaS API request/response schemas.
3. Changing registration workflow semantics beyond additive metadata.

## Proposed CLI UX

### New Top-Level Tracer Command Group
Add a first-class `roar tracer` command group.

#### Commands
1. `roar tracer status`
- Shows effective tracer policy.
- Shows backend readiness per backend (`ptrace`, `ebpf`) with actionable diagnostics.
- Shows proxy enablement state for run/build context.

2. `roar tracer set-default <auto|ptrace|ebpf>`
- Sets persistent default backend policy.

3. `roar tracer check [--backend <ptrace|ebpf>]`
- Runs active readiness checks and exits non-zero if selected backend is not ready.
- Intended for CI and environment validation.

4. `roar tracer setup ebpf [--path <binary>]`
- Existing eBPF capability setup behavior moved here (or mirrored), including capability verification and `perf_event_paranoid` checks.

### Runtime Overrides on Run/Build
Add runtime overrides:
1. `roar run --tracer <auto|ptrace|ebpf> ...`
2. `roar build --tracer <auto|ptrace|ebpf> ...`
3. `roar run --no-tracer-fallback ...`
4. `roar build --no-tracer-fallback ...`

Precedence:
1. CLI override
2. environment override (`ROAR_TRACER_DEFAULT`, optional)
3. persisted config

### Deprecated Command Removal
Remove deprecated tracer command aliases and require the new top-level surface:
1. remove `roar config tracer ...`
2. require `roar tracer ...` for tracer status/config/setup flows
3. keep `roar config` focused on generic key-value get/set/list behavior

## Configuration Model

### Proposed Config Keys
Replace/migrate `tracer.mode` with semantically clearer keys:

```toml
[tracer]
default = "auto"            # auto | ptrace | ebpf
fallback_enabled = true     # default true, especially meaningful for auto
```

### Migration Rules
1. Preserve current behavior by default (`auto`).
2. Persist only new keys on write.
3. Stop reading/writing legacy `tracer.mode`.

## Backend Selection Semantics

### Effective Selection
1. `ptrace`: force ptrace, fail fast if unavailable.
2. `ebpf`: force eBPF, fail fast if unavailable/unusable.
3. `auto`: choose first ready backend from preference order `[ebpf, ptrace]`.

### Readiness Checks
Introduce explicit readiness probes:
1. ptrace readiness:
- binary exists and is executable
2. eBPF readiness:
- binary exists and is executable
- required capabilities present (or process has sufficient privilege)
- `perf_event_paranoid <= 1` (or explicit warning/diagnostic path)

### Runtime Fallback
If fallback is enabled and primary backend fails before producing a valid tracer report:
1. log failure reason
2. try next backend once
3. preserve command env, including proxy endpoint env vars
4. annotate runtime metadata with fallback details

## Tracer Output Contract Alignment

## Canonical Internal Contract
Normalize both tracer outputs into a single internal model before provenance filtering:

```text
TracerDataNormalized:
  processes: list[process]
  files: list[{path, opened, read, written, chunks_read?, chunks_written?}]
  opened_files: list[str]
  read_files: list[str]
  written_files: list[str]
  start_time: float
  end_time: float
  tracer_mode: str
  version: int
```

### Parser/Adapter Strategy
Implement an adapter layer in provenance loading:
1. Accept ptrace format (`opened_files`, `read_files`, `written_files`, `processes`, ...)
2. Accept eBPF format (`files`, `processes`, `tracer_mode`, `version`, ...)
3. Derive missing aggregate arrays from `files` when needed.
4. Preserve chunk-level data in normalized structure for forward use.

### Tracer Emission Alignment
Update both tracers to include:
1. `version`
2. `tracer_mode` (`ptrace` or `ebpf`)
3. `files` records

ptrace may continue emitting aggregate arrays for compatibility, but ingestion should rely on normalized adapter logic.

## Proxy Lifecycle Requirements
Proxy behavior must be deterministic for all execution paths.

### Required Changes
1. Always stop per-run proxy in `finally` blocks, including:
- tracer-not-found errors
- tracer execution failures
- missing/invalid tracer report cases
2. Keep proxy env injection (`AWS_ENDPOINT_URL`) consistent across fallback attempts.
3. Guarantee that proxy log parsing is best-effort and never blocks run teardown.

### Expected Behavior
1. If proxy is enabled and starts successfully, it is always stopped.
2. If tracer backend changes due to fallback, proxy remains attached to the same run.
3. S3 lineage capture remains backend-agnostic.

## GLaaS API Compatibility

### Compatibility Contract
No changes to required GLaaS registration payloads:
1. session registration remains unchanged
2. job registration fields remain unchanged
3. artifact registration fields remain unchanged

### Optional Additive Metadata
Additive metadata only, under existing `metadata` JSON payload, for example:

```json
{
  "runtime": {
    "tracer": {
      "backend": "ptrace",
      "requested": "auto",
      "fallback_from": "ebpf",
      "fallback_reason": "capabilities_missing",
      "proxy_enabled": true
    }
  }
}
```

This is GLaaS-compatible because it does not alter API schema expectations.

## Implementation Plan

### Phase 1: Contract Normalization (highest priority)
1. Add tracer report adapter in provenance loader.
2. Ensure both current tracer outputs produce non-empty lineage inputs/outputs.
3. Add unit tests for both report formats.

### Phase 2: Runtime Selection and Fallback
1. Refactor tracer selection into explicit policy + readiness checks.
2. Implement controlled fallback behavior.
3. Ensure proxy env and lifecycle remain correct during fallback.

### Phase 3: CLI Redesign + Config Migration
1. Add `roar tracer` command group and subcommands.
2. Add `--tracer` and `--no-tracer-fallback` to run/build.
3. Use only `tracer.default` and `tracer.fallback_enabled` config keys.
4. Remove deprecated `roar config tracer ...` aliases.

### Phase 4: Documentation and Cleanup
1. Update README command documentation.
2. Document tracer policy semantics and migration behavior.
3. Remove dead code paths once migration window closes.

## Test Plan

### New Unit Tests
1. Tracer config keys:
- `tracer.default` persists and validates correctly
- `tracer.mode` is rejected as an unknown key
2. Tracer selection:
- `auto` picks ready eBPF first
- `auto` falls back to ptrace when eBPF not ready
- forced backends fail correctly when unavailable
3. Tracer report adapter:
- parses ptrace format
- parses eBPF format
- derives `read_files`/`written_files` from `files`
4. Proxy lifecycle:
- proxy stop called on tracer failure paths
- proxy env propagated through fallback

### New Integration Tests
1. `roar run` lineage with ptrace backend.
2. `roar run` lineage with eBPF backend (when available).
3. `roar run` with `--tracer auto` fallback scenario still records lineage.
4. `roar run` with proxy enabled captures S3 entries with both tracer modes.

### Existing Test Updates
1. Update config tests to include new tracer keys.
2. Update CLI tests for new `roar tracer` command group.
3. Remove compatibility tests for deleted aliases.
4. Update any tests assuming ptrace-only report fields.

## Acceptance Criteria
1. Both ptrace and eBPF backends can execute `roar run` and produce persisted lineage inputs/outputs.
2. `auto` mode behaves as documented (ready backend selection + optional fallback).
3. Proxy process never leaks on early failure paths.
4. Existing GLaaS registration tests continue to pass without API changes.
5. New tracer CLI commands are covered by tests and deprecated aliases are absent.

## Open Questions
1. Should `fallback_enabled` default to `false` for forced backends (`ptrace`/`ebpf`) even if globally true?
2. Should eBPF readiness enforce hard failure on `perf_event_paranoid > 1`, or allow best-effort attempt with warning?
3. Should chunk-level I/O data be persisted now, or only normalized and reserved for a later schema update?

## Decision Log
These decisions are accepted for this proposal and should guide implementation unless explicitly superseded.

1. Fallback behavior when tracer exits `0` but no tracer report is produced:
- Decision: do not retry another backend.
- Rationale: retrying would execute the command a second time and risks duplicate side effects. This is not safe by default for pipeline steps.

2. Forced `--tracer ebpf` when readiness checks indicate likely failure (missing caps or restrictive `perf_event_paranoid`):
- Decision: keep best-effort execution behavior (attempt run, fail naturally if backend cannot start).
- Rationale: explicit backend requests should honor operator intent, including environments where heuristics may be incomplete or intentionally bypassed.

3. Deprecation window for `roar config tracer ...` aliases:
- Decision: remove aliases now and require `roar tracer ...`.
- Rationale: simplify semantics and maintenance by keeping a single tracer command surface.

4. Default for `tracer.fallback_enabled`:
- Decision: default to `true`.
- Rationale: highest success probability for lineage capture in heterogeneous environments, while users can opt out per command with `--no-tracer-fallback`.

5. Chunk-level I/O handling:
- Decision: normalize and preserve chunk fields in internal tracer data now, defer persistence schema changes.
- Rationale: enables forward compatibility without forcing immediate DB/API changes.
