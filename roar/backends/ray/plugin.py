from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from roar.backends.ray.collector import collect_fragments
from roar.backends.ray.config import RAY_BACKEND_CONFIG
from roar.backends.ray.constants import RAY_STEP_NOISE_COMMANDS, RAY_TASK_COMMAND_PREFIXES
from roar.backends.ray.fragment_reconstituter import FragmentReconstituter
from roar.backends.ray.proxy_fragments import build_proxy_fragment
from roar.backends.ray.roar_worker import _run_worker_entrypoint, _startup
from roar.backends.ray.runtime_hooks import (
    initialize_runtime_process,
    observe_runtime_import,
    patch_imported_ray_module,
)
from roar.backends.ray.submit import (
    matches_ray_job_submit_command,
    plan_ray_job_submit_command,
)
from roar.execution.framework.contract import (
    DistributedRuntimeAdapter,
    DriverBootstrapAdapter,
    ExecutionBackend,
    ExecutionPolicyAdapter,
    FragmentReconstitutionAdapter,
    HostExecutionAdapter,
    RuntimeImportAdapter,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.registry import register_execution_backend
from roar.execution.runtime.host_execution import execute_host_run
from roar.execution.runtime.worker_bootstrap import build_packaged_worker_runtime_env

_GENERIC_WORKER_SETUP_HOOK = "roar.execution.runtime.worker_bootstrap.startup"
_GENERIC_WORKER_PY_EXECUTABLE = "roar-worker"


def ray_prepare_worker_runtime_env(
    runtime_env: Mapping[str, Any] | None,
    job_id: str,
    source_environ: Mapping[str, str],
) -> dict[str, Any]:
    del source_environ

    return build_packaged_worker_runtime_env(runtime_env, job_id)


def ray_should_start_driver_proxy(environ: Mapping[str, str]) -> bool:
    endpoint = str(environ.get("AWS_ENDPOINT_URL", "")).strip().lower()
    return (
        not endpoint
        or endpoint.startswith("http://127.0.0.1:")
        or endpoint.startswith("http://localhost:")
    )


def ray_build_driver_proxy_fragment(
    entries: Sequence[Any],
    started_at: float,
    ended_at: float,
    exit_code: int,
    environ: Mapping[str, str],
) -> dict[str, Any] | None:
    fragment = build_proxy_fragment(
        entries,
        function_name="s3_driver_proxy",
        task_id="proxy:driver",
        parent_job_uid=str(environ.get("ROAR_JOB_ID") or ""),
        started_at=started_at,
        ended_at=ended_at,
        exit_code=exit_code,
    )
    if fragment is None:
        return None
    return fragment.to_dict()


def ray_reconstituter_factory(
    session_id: str,
    token: str,
    glaas_url: str,
    roar_db_path: Path,
) -> FragmentReconstituter:
    return FragmentReconstituter(
        session_id=session_id,
        token=token,
        glaas_url=glaas_url,
        roar_db_path=roar_db_path,
    )


RAY_EXECUTION_BACKEND = ExecutionBackend(
    name="ray",
    priority=100,
    matches_command=matches_ray_job_submit_command,
    plan_command=plan_ray_job_submit_command,
    host_execution=HostExecutionAdapter(
        execute=execute_host_run,
    ),
    distributed=DistributedRuntimeAdapter(
        driver_bootstrap=DriverBootstrapAdapter(
            build_proxy_fragment=ray_build_driver_proxy_fragment,
            local_merge=collect_fragments,
            should_start_local_proxy=ray_should_start_driver_proxy,
        ),
        worker_bootstrap=WorkerBootstrapAdapter(
            py_executable=_GENERIC_WORKER_PY_EXECUTABLE,
            setup_hook=_GENERIC_WORKER_SETUP_HOOK,
            prepare_runtime_env=ray_prepare_worker_runtime_env,
            startup=_startup,
            run_entrypoint=_run_worker_entrypoint,
        ),
        fragment_reconstitution=FragmentReconstitutionAdapter(
            create_reconstituter=ray_reconstituter_factory,
        ),
        runtime_import=RuntimeImportAdapter(
            module_prefixes=("ray",),
            initialize_process=initialize_runtime_process,
            observe_import=observe_runtime_import,
            patch_module=patch_imported_ray_module,
        ),
    ),
    policy=ExecutionPolicyAdapter(
        noise_commands=RAY_STEP_NOISE_COMMANDS,
        task_command_prefixes=RAY_TASK_COMMAND_PREFIXES,
        job_environment_markers=("RAY_JOB_ID",),
        submit_roles=("submit",),
        task_roles=("task", "phase"),
        phase_roles=("phase",),
        noise_roles=("noise",),
    ),
    config=RAY_BACKEND_CONFIG,
)


def register() -> ExecutionBackend:
    register_execution_backend(RAY_EXECUTION_BACKEND)
    return RAY_EXECUTION_BACKEND

__all__ = [
    "RAY_EXECUTION_BACKEND",
    "ray_build_driver_proxy_fragment",
    "ray_prepare_worker_runtime_env",
    "ray_reconstituter_factory",
    "ray_should_start_driver_proxy",
    "register",
]
