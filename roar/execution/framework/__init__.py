"""Canonical shared execution framework imports."""

from roar.execution.framework.contract import (
    ROAR_EXECUTION_BACKEND_ENV,
    DistributedExecutionBackend,
    DriverBootstrapAdapter,
    FragmentReconstitutionAdapter,
    SubmitCommandRewrite,
    SubmitRunFinalizer,
    WorkerBootstrapAdapter,
)

__all__ = [
    "ROAR_EXECUTION_BACKEND_ENV",
    "DistributedExecutionBackend",
    "DriverBootstrapAdapter",
    "FragmentReconstitutionAdapter",
    "SubmitBackendAdapter",
    "SubmitCommandRewrite",
    "SubmitRunFinalizer",
    "WorkerBootstrapAdapter",
    "get_execution_backend",
    "iter_execution_backends",
    "maybe_rewrite_submit_command",
    "register_execution_backend",
]


def __getattr__(name: str):
    if name in {"get_execution_backend", "iter_execution_backends", "register_execution_backend"}:
        from roar.execution.framework import registry

        return getattr(registry, name)
    if name in {"SubmitBackendAdapter", "maybe_rewrite_submit_command"}:
        from roar.execution.framework import submit

        return getattr(submit, name)
    raise AttributeError(name)
