# Building A Distributed Execution Backend

This guide shows how to add a new distributed adapter on top of the shared execution-backend contract.

The goal is to keep new backends out of `run.py`, out of Ray-specific modules, and out of the fragment merge core. A backend should be a thin adapter that plugs its runtime into the shared execution framework.

See also:

- `docs/developer/ray-integration.md`
- `roar/execution/framework/__init__.py`
- `roar/execution/framework/contract.py`
- `roar/execution/framework/registry.py`
- `roar/execution/framework/submit.py`
- `roar/services/execution/driver_entrypoint.py`
- `roar/services/execution/worker_bootstrap.py`
- `roar/services/execution/fragment_reconstitution.py`

## 1. What The Framework Owns

The shared execution layer already owns:

- backend registration and discovery
- submit-command dispatch from `roar run`
- the wrapped driver entrypoint
- the worker bootstrap executable and setup-hook surface
- fragment session storage
- submit-time finalization and backend-driven reconstitution
- shared fragment transport, lineage merge, and cluster-bridge helpers

Your backend owns:

- how to recognize its submit command
- how to rewrite that submit command
- how its workers start
- how to adapt its fragments into the shared lineage model
- any backend-specific reconstitution logic

## 2. The Contract

The core contract lives in `roar/execution/framework/contract.py`:

```python
@dataclass(frozen=True)
class DistributedExecutionBackend:
    name: str
    matches_submit_command: SubmitCommandMatcher
    rewrite_submit_command: SubmitCommandRewriter
    driver_bootstrap: DriverBootstrapAdapter
    worker_bootstrap: WorkerBootstrapAdapter
    fragment_reconstitution: FragmentReconstitutionAdapter | None = None
```

In practice, that means a backend must answer five questions:

1. Does this backend claim the current submit command?
2. How should that command be rewritten so the job runs through Roar's shared driver entrypoint?
3. How should worker runtime env be prepared?
4. How do worker and driver fragments get emitted or merged?
5. If fragments are streamed remotely, how are they reconstituted after the submit command finishes?

## 3. Minimal Adapter Shape

A minimal backend module usually looks like this:

```python
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from roar.execution.framework.contract import (
    DistributedExecutionBackend,
    DriverBootstrapAdapter,
    FragmentReconstitutionAdapter,
    SubmitCommandRewrite,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.registry import register_execution_backend
from roar.services.execution.worker_bootstrap import build_packaged_worker_runtime_env


def demo_matches_submit_command(command: list[str]) -> bool:
    return command[:3] == ["demo", "job", "submit"]


def demo_rewrite_submit_command(command: list[str]) -> SubmitCommandRewrite:
    # Adjust this to your backend's CLI shape. The important part is that the
    # user entrypoint runs through the shared driver wrapper and the backend name
    # is present in the job environment.
    rewritten = list(command)
    return SubmitCommandRewrite(command=rewritten)


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
    raise SystemExit(f"replace with your backend's worker launch path: {argv}")


def demo_create_reconstituter(
    session_id: str,
    token: str,
    glaas_url: str,
    roar_db_path: Path,
):
    del session_id, token, glaas_url, roar_db_path
    raise NotImplementedError


DEMO_EXECUTION_BACKEND = DistributedExecutionBackend(
    name="demo",
    matches_submit_command=demo_matches_submit_command,
    rewrite_submit_command=demo_rewrite_submit_command,
    driver_bootstrap=DriverBootstrapAdapter(
        build_proxy_fragment=demo_build_driver_proxy_fragment,
        local_merge=demo_local_merge,
        should_start_local_proxy=lambda _env: False,
    ),
    worker_bootstrap=WorkerBootstrapAdapter(
        py_executable="roar-worker",
        setup_hook="roar.services.execution.worker_bootstrap.startup",
        prepare_runtime_env=demo_prepare_worker_runtime_env,
        startup=demo_worker_startup,
        run_entrypoint=demo_run_worker_entrypoint,
    ),
    fragment_reconstitution=FragmentReconstitutionAdapter(
        create_reconstituter=demo_create_reconstituter,
    ),
)


def register() -> DistributedExecutionBackend:
    register_execution_backend(DEMO_EXECUTION_BACKEND)
    return DEMO_EXECUTION_BACKEND
```

That skeleton is intentionally small:

- `matches_submit_command` claims the backend
- `rewrite_submit_command` adapts the submit CLI
- `driver_bootstrap` is where driver-local proxy capture hooks in if you need it
- `worker_bootstrap` tells Roar how to package and start workers
- `fragment_reconstitution` is only needed if fragments are streamed remotely and must be fetched later

## 4. The Critical Rewrite Rule

The submit rewrite is the most important step.

A working backend rewrite normally does all of the following:

- preserves the user's original submit semantics
- injects `ROAR_EXECUTION_BACKEND=<your-backend-name>`
- wraps the user entrypoint with `python -m roar.services.execution.driver_entrypoint -- ...`
- prepares worker runtime env through your backend's `prepare_runtime_env(...)`
- returns a `session_id` when remote fragment streaming is enabled

If you return a `session_id` and your backend provides `fragment_reconstitution`, `roar run` automatically attaches the shared submit finalizer for you. You do not need to wire that manually in `run.py`.

## 5. Packaging And Discovery

Backends are discovered through the `roar.execution_backends` entrypoint group:

```toml
[project.entry-points."roar.execution_backends"]
demo = "your_package.demo_backend:register"
```

Your `register()` function can return:

- one `DistributedExecutionBackend`
- `None`
- a list or tuple of `DistributedExecutionBackend` values

Built-in backends use the same mechanism. The Ray adapter is registered as:

```toml
[project.entry-points."roar.execution_backends"]
ray = "roar.backends.ray.plugin:register"
```

## 6. Driver And Worker Responsibilities

The shared runtime assumes these boundaries:

- `driver_entrypoint.py`
  - starts optional driver-local proxy capture
  - runs the user command
  - emits driver-local fragments through shared fragment transport
- `worker_bootstrap.py`
  - resolves the active backend from `ROAR_EXECUTION_BACKEND`
  - invokes backend startup
  - invokes the backend's worker entrypoint runner

Your adapter should not copy these modules. It should supply the callbacks that those shared modules call.

## 7. Simple First Backend Strategy

If you are building the next backend from scratch, the safest order is:

1. Implement command matching and submit rewrite.
2. Implement worker bootstrap with the shared `roar-worker` surface.
3. Get a single product-path e2e passing through `roar run <backend submit ...>`.
4. Add fragment streaming and reconstitution only after the execution path is stable.
5. Add driver-local proxy capture only if the backend has a real driver-local I/O path worth modeling.

This keeps the first slice small and avoids recreating the old Ray problem where backend-specific glue owned too much orchestration.

## 8. Recommended Test Shape

Use product-path tests first.

For a new backend, the minimum useful coverage is:

- one e2e that exercises `roar run <backend submit ...>` from the host
- one e2e that proves worker-side outputs reconstitute into `.roar/roar.db`
- one overlap or repeat e2e if the backend has sidecars, proxies, or per-node agents
- live coverage only after the e2e contract is stable

Unit tests are still useful, but they should stay local:

- matcher and rewrite edge cases
- worker runtime env shaping
- backend-specific fragment adaptation
- backend-specific reconstituter behavior

Do not treat mocked unit coverage as the main proof that a backend works.

## 9. A Practical Checklist

Before calling a new adapter done, verify:

- the backend is registered through `roar.execution_backends`
- `matches_submit_command` only claims the intended command family
- the submit rewrite sets `ROAR_EXECUTION_BACKEND`
- the user entrypoint runs through `roar.services.execution.driver_entrypoint`
- worker env is prepared through the backend contract, not inline in `run.py`
- product-path e2e validates host submit, worker capture, and reconstitution
- backend-specific code stays in the adapter layer rather than leaking into shared modules

## 10. Ray As The Reference Implementation

Use the Ray backend as the reference implementation for a full adapter:

- backend registration: `roar/backends/ray/plugin.py`
- submit rewrite: `roar/backends/ray/submit.py`
- worker runtime env helpers: `roar/ray/env_contract.py`
- submit context helpers: `roar/ray/submit_context.py`
- worker runtime implementation: `roar/ray/roar_worker.py`
- backend-specific reconstitution: `roar/ray/fragment_reconstituter.py`
- backend-specific lineage adapter: `roar/ray/collector.py`

Start from the shared contract first, then borrow only the backend-specific patterns you actually need.
