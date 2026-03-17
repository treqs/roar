from __future__ import annotations

from roar.backends.osmo.config import OSMO_BACKEND_CONFIG
from roar.backends.osmo.host_execution import execute_osmo_workflow_submit
from roar.backends.osmo.submit import (
    matches_osmo_workflow_submit_command,
    plan_osmo_workflow_submit_command,
)
from roar.execution.framework.contract import (
    ExecutionBackend,
    ExecutionPolicyAdapter,
    HostExecutionAdapter,
)
from roar.execution.framework.registry import register_execution_backend

OSMO_EXECUTION_BACKEND = ExecutionBackend(
    name="osmo",
    priority=90,
    matches_command=matches_osmo_workflow_submit_command,
    plan_command=plan_osmo_workflow_submit_command,
    host_execution=HostExecutionAdapter(execute=execute_osmo_workflow_submit),
    policy=ExecutionPolicyAdapter(
        submit_roles=("submit",),
    ),
    config=OSMO_BACKEND_CONFIG,
)


def register() -> ExecutionBackend:
    register_execution_backend(OSMO_EXECUTION_BACKEND)
    return OSMO_EXECUTION_BACKEND


__all__ = [
    "OSMO_EXECUTION_BACKEND",
    "register",
]
