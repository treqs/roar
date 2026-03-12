"""Compatibility shim over the canonical execution framework."""

from roar.execution.framework.contract import (
    ROAR_EXECUTION_BACKEND_ENV,
    DistributedExecutionBackend,
    DriverBootstrapAdapter,
    FragmentReconstitutionAdapter,
    SubmitCommandRewrite,
    SubmitRunFinalizer,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.registry import (
    get_execution_backend,
    iter_execution_backends,
    register_execution_backend,
)

__all__ = [
    "ROAR_EXECUTION_BACKEND_ENV",
    "DistributedExecutionBackend",
    "DriverBootstrapAdapter",
    "FragmentReconstitutionAdapter",
    "SubmitCommandRewrite",
    "SubmitRunFinalizer",
    "WorkerBootstrapAdapter",
    "get_execution_backend",
    "iter_execution_backends",
    "register_execution_backend",
]
