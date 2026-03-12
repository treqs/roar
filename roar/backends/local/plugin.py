from __future__ import annotations

from roar.execution.framework.contract import (
    ExecutionBackend,
    ExecutionCommandPlan,
    ExecutionPolicyAdapter,
    HostExecutionAdapter,
)
from roar.execution.framework.registry import register_execution_backend
from roar.services.execution.host_execution import execute_host_run


def local_matches_command(_command: list[str]) -> bool:
    return True


def local_plan_command(command: list[str]) -> ExecutionCommandPlan:
    return ExecutionCommandPlan(
        backend_name="local",
        command=list(command),
        execution_role="host",
    )


LOCAL_EXECUTION_BACKEND = ExecutionBackend(
    name="local",
    priority=-100,
    matches_command=local_matches_command,
    plan_command=local_plan_command,
    host_execution=HostExecutionAdapter(execute=execute_host_run),
    policy=ExecutionPolicyAdapter(
        host_roles=("host",),
    ),
)


def register() -> ExecutionBackend:
    register_execution_backend(LOCAL_EXECUTION_BACKEND)
    return LOCAL_EXECUTION_BACKEND


register()

__all__ = [
    "LOCAL_EXECUTION_BACKEND",
    "local_matches_command",
    "local_plan_command",
    "register",
]
