# Building An Execution Backend

This guide shows how to add a backend on top of Roar's execution framework.

The framework is intentionally broader than Ray. It owns command planning, backend registration, host execution, distributed runtime bootstrapping, fragment-session finalization, and backend config discovery. A backend should plug into those contracts instead of pushing new backend-specific conditionals into shared code.

See also:

- `docs/developer/ray-integration.md`
- `roar/execution/framework/contract.py`
- `roar/execution/framework/planning.py`
- `roar/execution/framework/registry.py`
- `roar/execution/runtime/driver_entrypoint.py`
- `roar/execution/runtime/worker_bootstrap.py`
- `roar/execution/fragments/reconstitution.py`

## 1. What The Framework Owns

Shared framework code owns:

- backend registration and discovery
- command planning from `roar run` and `roar build`
- host-side execution handoff
- distributed driver bootstrap
- worker bootstrap dispatch
- fragment-session storage and submit finalization
- shared fragment transport, lineage merge, and cluster-bridge helpers
- backend-owned config registration into `roar config` and `roar init`

Your backend owns:

- how to recognize commands that belong to it
- how to rewrite those commands, if they need backend-specific wrapping
- how host execution should run, if it differs from the shared local path
- any distributed runtime hooks it needs
- backend-specific fragment shaping and reconstitution
- backend-specific config schema and defaults

## 2. The Contract

The core contract lives in `roar/execution/framework/contract.py`:

```python
@dataclass(frozen=True)
class ExecutionBackend:
    name: str
    priority: int = 0
    matches_command: CommandMatcher = lambda _command: False
    plan_command: CommandPlanner = ...
    host_execution: HostExecutionAdapter = ...
    distributed: DistributedRuntimeAdapter | None = None
    policy: ExecutionPolicyAdapter | None = None
    config: BackendConfigAdapter | None = None
```

The planner returns:

```python
@dataclass(frozen=True)
class ExecutionCommandPlan:
    backend_name: str
    command: list[str]
    session_id: str | None = None
    finalize_run: SubmitRunFinalizer | None = None
```

That gives each backend one clear job:

1. claim the command family it owns
2. return the canonical command plan for that command
3. provide a host execution path
4. optionally provide distributed runtime hooks
5. optionally expose policy and config metadata

## 3. Minimal Local Backend

The smallest useful backend is a host-only backend:

```python
from roar.execution.framework.contract import (
    ExecutionBackend,
    ExecutionCommandPlan,
    HostExecutionAdapter,
)
from roar.execution.framework.registry import register_execution_backend
from roar.execution.runtime.host_execution import execute_host_run


def local_matches_command(_command: list[str]) -> bool:
    return True


def local_plan_command(command: list[str]) -> ExecutionCommandPlan:
    return ExecutionCommandPlan(
        backend_name="local",
        command=list(command),
    )


LOCAL_EXECUTION_BACKEND = ExecutionBackend(
    name="local",
    priority=-100,
    matches_command=local_matches_command,
    plan_command=local_plan_command,
    host_execution=HostExecutionAdapter(execute=execute_host_run),
)


def register() -> ExecutionBackend:
    register_execution_backend(LOCAL_EXECUTION_BACKEND)
    return LOCAL_EXECUTION_BACKEND
```

That is enough to route plain `roar run python ...` or `roar build ...` through the same backend planner as distributed adapters.

## 4. Minimal Distributed Backend

A distributed backend adds a `DistributedRuntimeAdapter`:

```python
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from roar.execution.framework.contract import (
    DistributedRuntimeAdapter,
    DriverBootstrapAdapter,
    ExecutionBackend,
    ExecutionCommandPlan,
    FragmentReconstitutionAdapter,
    HostExecutionAdapter,
    RuntimeImportAdapter,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.registry import register_execution_backend
from roar.execution.runtime.host_execution import execute_host_run
from roar.execution.runtime.worker_bootstrap import build_packaged_worker_runtime_env


def demo_matches_command(command: list[str]) -> bool:
    return command[:3] == ["demo", "job", "submit"]


def demo_plan_command(command: list[str]) -> ExecutionCommandPlan:
    rewritten = list(command)
    return ExecutionCommandPlan(
        backend_name="demo",
        command=rewritten,
        session_id="optional-fragment-session",
    )


def demo_prepare_worker_runtime_env(
    runtime_env: Mapping[str, Any] | None,
    job_id: str,
    source_environ: Mapping[str, str],
) -> dict[str, Any]:
    del source_environ
    return build_packaged_worker_runtime_env(runtime_env, job_id)


def demo_build_driver_proxy_fragment(
    entries: Sequence[Any],
    started_at: float,
    ended_at: float,
    exit_code: int,
    environ: Mapping[str, str],
) -> dict[str, Any] | None:
    del entries, started_at, ended_at, exit_code, environ
    return None


def demo_local_merge(
    fragments: list[dict[str, Any]],
    project_dir: str,
    driver_job_uid: str | None,
) -> None:
    del fragments, project_dir, driver_job_uid


def demo_worker_startup() -> None:
    return None


def demo_run_worker_entrypoint(argv: list[str]) -> None:
    raise SystemExit(f"replace with your backend worker launch path: {argv}")


def demo_create_reconstituter(
    session_id: str,
    token: str,
    glaas_url: str,
    roar_db_path: Path,
):
    del session_id, token, glaas_url, roar_db_path
    raise NotImplementedError


DEMO_EXECUTION_BACKEND = ExecutionBackend(
    name="demo",
    priority=100,
    matches_command=demo_matches_command,
    plan_command=demo_plan_command,
    host_execution=HostExecutionAdapter(execute=execute_host_run),
    distributed=DistributedRuntimeAdapter(
        driver_bootstrap=DriverBootstrapAdapter(
            build_proxy_fragment=demo_build_driver_proxy_fragment,
            local_merge=demo_local_merge,
            should_start_local_proxy=lambda _env: False,
        ),
        worker_bootstrap=WorkerBootstrapAdapter(
            py_executable="roar-worker",
            setup_hook="roar.execution.runtime.worker_bootstrap.startup",
            prepare_runtime_env=demo_prepare_worker_runtime_env,
            startup=demo_worker_startup,
            run_entrypoint=demo_run_worker_entrypoint,
        ),
        fragment_reconstitution=FragmentReconstitutionAdapter(
            create_reconstituter=demo_create_reconstituter,
        ),
        runtime_import=RuntimeImportAdapter(
            module_prefixes=("demo",),
        ),
    ),
)


def register() -> ExecutionBackend:
    register_execution_backend(DEMO_EXECUTION_BACKEND)
    return DEMO_EXECUTION_BACKEND
```

If the planner sees both a specific distributed backend and the fallback local backend, priority decides which one wins.

## 5. Command Planning Rules

The planner lives in `roar/execution/framework/planning.py`.

Important rules:

- `matches_command` should be narrow for distributed backends and broad only for fallbacks like `local`
- `plan_command` should return the final command that Roar will execute
- `backend_name` must be set in the returned `ExecutionCommandPlan`
- if a distributed backend returns a `session_id` and does not provide its own `finalize_run`, the framework attaches the shared submit finalizer automatically

The planner is now the single place where `roar run` and `roar build` choose a backend.

## 6. Host And Distributed Responsibilities

`host_execution` owns the host-side command path. The shared helper is `roar.execution.runtime.host_execution.execute_host_run`.

`distributed` owns distributed-only hooks:

- `driver_bootstrap`
  - driver-local proxy fragment construction
  - driver-local merge
  - optional decision for starting a local proxy
- `worker_bootstrap`
  - worker runtime env shaping
  - startup hook
  - final worker entrypoint execution
- `fragment_reconstitution`
  - remote fragment fetch/reconstitution after submit completes
- `runtime_import`
  - backend module prefix matching
  - process initialization
  - import observation
  - matched-module patching

If a backend does not need distributed behavior, leave `distributed=None`.

## 7. Packaging And Discovery

Built-in backends are discovered from `roar.backends.*.plugin`.

External backends are discovered through the `roar.execution_backends` entrypoint group:

```toml
[project.entry-points."roar.execution_backends"]
demo = "your_package.demo_backend:register"
```

Your `register()` function can return:

- one `ExecutionBackend`
- `None`
- a list or tuple of `ExecutionBackend` values

## 8. Backend-Owned Config

If a backend needs config, keep that ownership inside the backend package by returning a `BackendConfigAdapter`.

Use it for:

- section defaults
- `roar config list` / `roar config set` metadata
- `roar init` template fragments
- normalization of backend config sections

Core config models should not gain new backend-specific fields.

## 9. Recommended Test Shape

Prefer behavior guarantees over mocked coverage.

For a new backend, the minimum useful coverage is:

- one product-path test proving the framework selects the backend for a real command
- one e2e that exercises the backend through `roar run ...`
- one e2e that proves captured behavior reconstitutes into `.roar/roar.db`
- one overlap/repeat e2e if the backend has agents, sidecars, or per-node resources
- live coverage only after the e2e path is stable

Localized unit tests are still useful for:

- command matching edge cases
- command planning edge cases
- backend-specific runtime env shaping
- backend-specific fragment adaptation
- backend-specific reconstituter behavior

Do not treat mocked unit tests as the main proof that a backend works.

## 10. Checklist

Before calling a backend done, verify:

- it is registered through the execution framework
- it does not require new backend-specific conditionals in shared code
- its command planning path sets `ROAR_EXECUTION_BACKEND`
- it uses shared host execution or an explicit backend-owned host adapter
- distributed runtime hooks live under `distributed`, not inline in CLI code
- product-path tests validate the canonical backend path
- backend-specific config lives in the backend package

## 11. Reference Implementations

Use these as the current references:

- local backend:
  - `roar/backends/local/plugin.py`
- Ray backend:
  - `roar/backends/ray/plugin.py`
  - `roar/backends/ray/submit.py`
  - `roar/backends/ray/submit_context.py`
  - `roar/backends/ray/env_contract.py`
  - `roar/backends/ray/runtime_hooks.py`
  - `roar/backends/ray/roar_worker.py`
  - `roar/backends/ray/fragment_reconstituter.py`
  - `roar/backends/ray/collector.py`

Start from the framework contract first, then add backend-owned pieces only where the contract says they belong.
