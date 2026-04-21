# Extension Architecture Refactor Plan

Companion to:
- `/home/trevor/dev/PLUGIN_DESIGN.md`

## Summary

This proposal turns the updated extension-architecture design into a concrete refactor roadmap for `roar`.

The goal is not to build a generic plugin system. The goal is to make `roar` easier to maintain and extend by tightening the extension seams that already exist:

1. **execution backends** for alternate execution semantics
2. **typed provider registries** for provider-shaped integrations
3. **static lazy CLI commands** unless command growth justifies a very small command-set seam later

The main maintainability problems in the current codebase are not a lack of generic plugin machinery. They are a handful of specific boundary leaks:

- proxy lifecycle logic still lives in the shared host execution path
- bootstrap still mixes core initialization with concrete provider registration
- optional provider discovery still uses one broad entry-point group
- the CLI has no narrow command-contribution seam if optional command groups multiply

This plan addresses those issues in small, independently shippable phases.

## Goals

1. Keep the local-first tracked execution path small and predictable.
2. Make extension points explicit, typed, and easy to test.
3. Preserve the existing execution-backend framework as the primary extension surface.
4. Reduce direct knowledge of optional runtime resources inside shared host execution.
5. Keep labels, lineage, composites, and persistence as core product semantics.
6. Avoid introducing a generic hook bus or plugin manager.
7. Sequence changes so each phase is independently deployable and low-risk.

## Non-goals

1. Replacing the current execution-backend framework.
2. Moving GLaaS flows behind a new generic plugin abstraction.
3. Generalizing all CLI commands into dynamically discovered plugins.
4. Changing the persistence model for labels, lineage, artifacts, or composites.
5. Refactoring Ray and OSMO behavior unless needed to support clearer shared seams.

## Current-state audit

### What is already in good shape

#### 1. Execution backends are the right extension seam

The current backend framework already owns the important execution-planning surface:

- `roar/execution/framework/contract.py`
- `roar/execution/framework/planning.py`
- `roar/execution/framework/registry.py`
- `roar/backends/local/plugin.py`
- `roar/backends/ray/plugin.py`
- `roar/backends/osmo/plugin.py`

This is the correct abstraction for:

- command matching
- command rewriting
- host execution dispatch
- distributed runtime bootstrap
- runtime import behavior
- fragment reconstitution
- backend-owned config

This framework should be preserved and tightened, not replaced.

#### 2. Labels and lineage semantics are correctly core-owned

Current core/application ownership of labels and publish-time label sync is the right long-term boundary:

- `roar/application/labels.py`
- `roar/cli/commands/label.py`
- `roar/application/publish/registration.py`

#### 3. Static lazy CLI loading is still a good default

`roar/cli/__init__.py` keeps startup and `--help` fast while making top-level commands easy to audit. That is still the right default.

### What currently leaks

#### Leak A: proxy lifecycle in shared host execution

The current shared host execution path still decides whether proxy support is enabled and instantiates `ProxyService` directly:

- `roar/execution/runtime/host_execution.py`
- `roar/execution/runtime/coordinator.py`

The coordinator then owns:

- resource startup
- env patching
- teardown
- S3 observation collection

That means a specific optional runtime resource is still coupled to the generic host path.

#### Leak B: bootstrap mixes core init and provider wiring

`roar/core/bootstrap.py` currently does all of the following:

- configures logging
- imports concrete built-in providers
- registers built-in providers
- discovers optional providers

That works, but it mixes application bootstrap with concrete integration registration.

#### Leak C: one broad provider entry-point group

`roar/integrations/discovery.py` currently discovers optional integrations via the generic `roar.integrations` entry-point group.

That is workable for now, but over time a single catch-all group becomes less maintainable than typed groups.

#### Leak D: no narrow future seam for command-family growth

The CLI is correctly static today, but if more backend-owned or optional command families appear, `roar` will need a small command-set seam. The important thing is to add that only if there is a real need, and to keep it separate from execution and provider concerns.

## Architecture decision

The accepted long-term strategy is:

- keep **execution backends** as the main extensibility model
- keep **provider registries** narrow and typed
- keep **CLI commands static** unless growth justifies a small command-set registry
- keep **remote lineage semantics built-in** unless a second remote registry target appears
- keep **labels, lineage, composites, and persistence in core/application**
- do **not** introduce a generic plugin manager or hook bus

## Success criteria

This refactor is successful when:

1. `roar/execution/runtime/host_execution.py` no longer directly branches on `proxy.enabled` and constructs `ProxyService` inline.
2. `RunCoordinator` no longer contains proxy-specific lifecycle logic.
3. `roar/core/bootstrap.py` no longer imports concrete built-in provider implementations directly.
4. provider discovery can distinguish telemetry and VCS entry points explicitly.
5. no new generic lifecycle hook bus or plugin manifest abstraction is introduced.
6. the current execution-backend tests and integration-discovery tests still pass.
7. the local product path remains the default, simple path.

## Refactor plan

## Phase 1: Extract host runtime resources from shared host execution

### Why this phase comes first

This is the most important maintainability fix because it removes the most obvious optional-feature branching from the shared host path without changing the execution-backend model.

### Proposed design

Introduce a **small host runtime-resource contract** for resources that must be started around a local host execution.

This should be intentionally narrower than a hook system.

Suggested shape:

```python
@dataclass(frozen=True)
class RuntimeResourceStart:
    env: Mapping[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class RuntimeResourceStop:
    observations: Mapping[str, Any] = field(default_factory=dict)

class HostRuntimeResource(Protocol):
    name: str

    def start(self, ctx: RunContext, environ: Mapping[str, str]) -> RuntimeResourceStart: ...
    def stop(self, *, exit_code: int | None) -> RuntimeResourceStop: ...
```

The first concrete implementation should be:

- `ProxyRuntimeResource`

Responsibilities:

- decide enablement from config or explicit runtime selection
- start and stop `ProxyService`
- provide `AWS_ENDPOINT_URL` / `ROAR_UPSTREAM_S3_ENDPOINT` env patches
- return collected S3 observations on stop

The coordinator should know only that it is working with runtime resources and resource observations. It should not know about proxy-specific startup rules.

### Important constraint

Do **not** generalize this into a cross-cutting lifecycle hook bus. This seam is only for host runtime resources with explicit startup, env patch, and teardown semantics.

### Likely files

Core implementation:
- `roar/execution/runtime/host_execution.py`
- `roar/execution/runtime/coordinator.py`
- new: `roar/execution/runtime/resources.py`
- new: `roar/execution/runtime/proxy_resource.py` or `roar/execution/cluster/proxy_resource.py`

Proxy code reused:
- `roar/execution/cluster/proxy.py`

Potential shared helper cleanup:
- `roar/execution/runtime/driver_entrypoint.py`

Tests to add/update:
- `tests/unit/test_proxy_coordinator.py`
- `tests/unit/test_proxy_service.py`
- `tests/integration/test_proxy_integration.py`
- `tests/backends/ray/unit/test_driver_entrypoint.py`

### Acceptance criteria

1. `execute_host_run()` no longer directly checks `proxy.enabled`.
2. `RunCoordinator` no longer has proxy-specific env/start/stop branches.
3. proxy lifecycle remains deterministic on success and failure paths.
4. existing S3 lineage behavior stays unchanged.
5. Ray driver proxy tests still pass.

### PR boundary

This should be one PR.

It is cohesive because it removes one specific boundary leak without changing backend contracts or CLI behavior.

## Phase 2: Move provider wiring out of core bootstrap

### Why this phase is next

After runtime cleanup, the next biggest clarity win is separating core process bootstrap from integration registration.

### Proposed design

Keep `roar.core.bootstrap.bootstrap()` responsible for:

- one-time initialization
- logging setup
- calling a dedicated integration bootstrap helper

Move provider registration/discovery into an explicit integration bootstrap module, for example:

- `roar/integrations/bootstrap.py`

Suggested functions:

- `register_builtin_providers()`
- `discover_optional_providers()`
- `bootstrap_integrations()`

This keeps concrete provider wiring in the integrations layer rather than the core bootstrap layer.

### Important constraint

Do not turn bootstrap into a plugin manager. This change is about moving concrete provider ownership to the right package, not about adding more generalized architecture.

### Likely files

Core/bootstrap:
- `roar/core/bootstrap.py`

New/updated integration files:
- new: `roar/integrations/bootstrap.py`
- `roar/integrations/__init__.py`
- `roar/integrations/discovery.py`
- `roar/integrations/registry.py`

Tests to add/update:
- `tests/integration/test_bootstrap_integrations.py`
- `tests/integrations/test_integration_discovery.py`
- `tests/unit/test_bootstrap_config_path.py`

### Acceptance criteria

1. `roar/core/bootstrap.py` no longer imports `GitVCSProvider` or `WandBTelemetryProvider` directly.
2. built-in provider registration still happens exactly once.
3. bootstrap remains lazy and deterministic.
4. current provider-discovery tests still pass.

### PR boundary

This should be one PR after Phase 1.

## Phase 3: Introduce typed provider entry-point groups

### Why this phase matters

A single `roar.integrations` entry-point group is convenient early on, but it becomes harder to validate and reason about as integration types grow.

Typed groups make extension clearer without inventing a generic plugin host.

### Proposed design

Add typed entry-point groups such as:

- `roar.telemetry_providers`
- `roar.vcs_providers`

Discovery should support both:

- new typed groups
- the legacy `roar.integrations` group during a compatibility window

The new preferred path should be typed groups. The legacy group should remain supported temporarily for backwards compatibility.

### Important constraint

Keep discovery type-specific. Do not reintroduce a manifest model that mixes commands, runtime hooks, config, and providers into one registration payload.

### Likely files

Packaging and discovery:
- `pyproject.toml`
- `roar/integrations/discovery.py`
- `roar/integrations/registry.py`

Docs/tests:
- `docs/developer/execution-backend-adapter.md`
- `tests/integrations/test_integration_discovery.py`
- possibly new targeted tests for typed-group loading

### Acceptance criteria

1. telemetry and VCS providers can be discovered through typed groups.
2. legacy `roar.integrations` discovery continues to work during migration.
3. provider validation stays explicit.
4. no runtime behavior changes for users who do not install optional providers.

### PR boundary

This should be one additive compatibility PR.

## Phase 4: Add a command-set registry only if the CLI crosses a clear threshold

### Decision gate

Do **not** implement this phase now unless one of these becomes true:

1. there are at least three backend-owned or optional command families beyond the current static set, or
2. a third-party package needs to contribute top-level commands, or
3. static `LAZY_COMMANDS` ownership becomes a recurring maintenance problem

### If the gate is met

Introduce a small command-set registry with a narrow contract only:

```python
@dataclass(frozen=True)
class CommandSetSpec:
    name: str
    module_path: str
    attr_name: str
    short_help: str
    help_section: str | None = None
```

This registry should handle only:

- command contribution
- lazy loading
- help grouping

It should **not** also own runtime hooks, backend registration, or provider wiring.

### Likely files if triggered

- `roar/cli/__init__.py`
- new: `roar/cli/command_registry.py`
- `pyproject.toml` if external command sets are allowed
- tests around help rendering and lazy loading

### Acceptance criteria if triggered

1. startup/help remains lazy.
2. command contribution stays separate from backend/provider contracts.
3. `LAZY_COMMANDS` can be partly generated from contributed command specs.
4. no generic plugin host appears.

### PR boundary

Only proceed if the trigger is hit.

## Phase 5: Abstract remote registry transport only if a second remote target appears

### Decision gate

Do **not** implement this phase unless `roar` must support something meaningfully different from GLaaS.

### Why this is deferred

GLaaS is still part of `roar`’s main product story. Abstracting it too early would add indirection without solving a real problem.

### If the gate is met

Add a narrow remote registry transport contract for:

- session registration
- job registration
- artifact registration
- label sync
- fragment transport/finalization

Do not expand this into a generic plugin host.

### Likely files if triggered

- `roar/application/publish/*`
- `roar/integrations/glaas/*`
- new transport contract module under `roar/application/publish/` or `roar/core/interfaces/`
- publish/get/reproduce tests

## What not to do

To keep this effort aligned with the maintainability goal, do **not** do the following:

1. Do not add `roar.plugins.*`.
2. Do not add a plugin manifest type.
3. Do not add a generic hook bus for run lifecycle stages.
4. Do not let optional extensions mutate recorder or DB behavior indirectly.
5. Do not move labels or lineage semantics out of core.
6. Do not abstract GLaaS behind a transport contract until there is a second real target.
7. Do not add a command registry unless the CLI actually needs one.

## Verification plan

Every phase should run the standard `roar` gates before merge.

Baseline repo gates:

```bash
pytest -m "not live_glaas and not ebpf"
ruff check .
mypy roar
```

Targeted fast checks for this refactor track:

```bash
.venv/bin/pytest \
  tests/execution/framework/test_execution_framework_layout.py \
  tests/integration/test_bootstrap_integrations.py \
  tests/integrations/test_integration_discovery.py \
  tests/unit/test_proxy_coordinator.py \
  tests/backends/ray/unit/test_driver_entrypoint.py
```

If proxy/resource behavior changes materially, also run:

```bash
.venv/bin/pytest \
  tests/integration/test_proxy_integration.py \
  tests/integration/test_proxy_cli_integration.py
```

## Recommended rollout order

1. **Phase 1**: runtime-resource extraction for proxy
2. **Phase 2**: provider bootstrap cleanup
3. **Phase 3**: typed provider entry-point groups with compatibility fallback
4. **Phase 4**: command-set registry only if the CLI crosses the trigger
5. **Phase 5**: remote registry abstraction only if a second target appears

This order gives the biggest maintainability win first while keeping each step small and safe.

## Suggested first PR

If work starts immediately, the first PR should be:

**`refactor(runtime): extract proxy lifecycle from shared host execution`**

Scope:

- introduce the host runtime-resource seam
- move proxy startup/env/teardown out of `RunCoordinator`
- keep user-facing behavior unchanged
- add/update targeted proxy lifecycle tests

That is the most concrete step toward making `roar` easier to maintain without backsliding into a generic plugin architecture.
