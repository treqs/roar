"""Canonical shared execution framework imports."""

from roar.execution.framework.contract import (
    ROAR_EXECUTION_BACKEND_ENV,
    DistributedRuntimeAdapter,
    DriverBootstrapAdapter,
    ExecutionBackend,
    ExecutionCommandPlan,
    FragmentReconstitutionAdapter,
    HostExecutionAdapter,
    SubmitRunFinalizer,
    WorkerBootstrapAdapter,
)

__all__ = [
    "ROAR_EXECUTION_BACKEND_ENV",
    "DistributedRuntimeAdapter",
    "DriverBootstrapAdapter",
    "ExecutionBackend",
    "ExecutionCommandPlan",
    "FragmentReconstitutionAdapter",
    "HostExecutionAdapter",
    "SubmitRunFinalizer",
    "WorkerBootstrapAdapter",
    "get_execution_backend",
    "iter_execution_backends",
    "plan_execution_command",
    "register_execution_backend",
]


def __getattr__(name: str):
    if name in {"get_execution_backend", "iter_execution_backends", "register_execution_backend"}:
        from roar.execution.framework import registry

        return getattr(registry, name)
    if name == "plan_execution_command":
        from roar.execution.framework import planning

        return getattr(planning, name)
    raise AttributeError(name)
