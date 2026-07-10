from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from roar.backends.k8s.config import K8S_BACKEND_CONFIG
from roar.backends.k8s.fragment_reconstituter import create_k8s_fragment_reconstituter
from roar.backends.k8s.host_execution import execute_k8s_job_submit
from roar.backends.k8s.submit import (
    matches_kubectl_job_submit_command,
    plan_kubectl_job_submit_command,
)
from roar.execution.framework.contract import (
    DistributedRuntimeAdapter,
    DriverBootstrapAdapter,
    ExecutionBackend,
    ExecutionPolicyAdapter,
    FragmentReconstitutionAdapter,
    HostExecutionAdapter,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.registry import register_execution_backend


def _no_driver_proxy_fragment(
    entries: Sequence[Any],
    started_at: float,
    ended_at: float,
    exit_code: int,
    environ: Mapping[str, str],
) -> dict[str, Any] | None:
    del entries, started_at, ended_at, exit_code, environ
    return None


def _no_local_merge(
    fragments: list[dict[str, Any]],
    project_dir: str,
    driver_job_uid: str | None,
) -> None:
    del fragments, project_dir, driver_job_uid


def _passthrough_runtime_env(
    runtime_env: Mapping[str, Any] | None,
    job_id: str,
    source_environ: Mapping[str, str],
) -> dict[str, Any]:
    del job_id, source_environ
    return dict(runtime_env or {})


def _worker_startup() -> None:
    return None


def _worker_entrypoint(argv: list[str]) -> None:
    raise RuntimeError(
        "the k8s backend instruments pods via manifest rewriting; "
        f"roar-worker entrypoints are not used (argv: {argv!r})"
    )


K8S_EXECUTION_BACKEND = ExecutionBackend(
    name="k8s",
    priority=95,
    matches_command=matches_kubectl_job_submit_command,
    plan_command=plan_kubectl_job_submit_command,
    host_execution=HostExecutionAdapter(execute=execute_k8s_job_submit),
    distributed=DistributedRuntimeAdapter(
        driver_bootstrap=DriverBootstrapAdapter(
            build_proxy_fragment=_no_driver_proxy_fragment,
            local_merge=_no_local_merge,
            should_start_local_proxy=lambda _env: False,
        ),
        worker_bootstrap=WorkerBootstrapAdapter(
            py_executable="roar-worker",
            setup_hook="roar.execution.runtime.worker_bootstrap.startup",
            prepare_runtime_env=_passthrough_runtime_env,
            startup=_worker_startup,
            run_entrypoint=_worker_entrypoint,
        ),
        fragment_reconstitution=FragmentReconstitutionAdapter(
            create_reconstituter=create_k8s_fragment_reconstituter,
        ),
    ),
    policy=ExecutionPolicyAdapter(
        submit_roles=("submit",),
        task_roles=("task",),
        job_environment_markers=("ROAR_K8S_PARENT_JOB_UID",),
    ),
    config=K8S_BACKEND_CONFIG,
)


def register() -> ExecutionBackend:
    register_execution_backend(K8S_EXECUTION_BACKEND)
    return K8S_EXECUTION_BACKEND


__all__ = [
    "K8S_EXECUTION_BACKEND",
    "register",
]
