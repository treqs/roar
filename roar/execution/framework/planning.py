"""Canonical command-planning surface for execution backends."""

from __future__ import annotations

from roar.execution.framework.contract import ExecutionCommandPlan
from roar.services.execution.fragment_reconstitution import build_submit_finalizer


def plan_execution_command(command: list[str]) -> ExecutionCommandPlan:
    from roar.execution.framework.registry import iter_execution_backends

    for backend in iter_execution_backends():
        if not backend.matches_command(command):
            continue
        planned = backend.rewrite_command(command)
        if not planned.backend_name:
            planned = ExecutionCommandPlan(
                backend_name=backend.name,
                command=list(planned.command),
                session_id=planned.session_id,
                finalize_run=planned.finalize_run,
            )
        distributed = backend.distributed
        if (
            planned.finalize_run is None
            and planned.session_id
            and distributed is not None
            and distributed.fragment_reconstitution is not None
        ):
            planned = ExecutionCommandPlan(
                backend_name=planned.backend_name,
                command=list(planned.command),
                session_id=planned.session_id,
                finalize_run=build_submit_finalizer(planned.backend_name, planned.session_id),
            )
        return planned
    raise LookupError(f"no execution backend matched command: {command!r}")


__all__ = [
    "ExecutionCommandPlan",
    "plan_execution_command",
]
