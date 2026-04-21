from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import ExecutionSetupError

if TYPE_CHECKING:
    from roar.core.models.run import RunContext, RunResult


def execute_host_run(ctx: RunContext) -> RunResult:
    from roar.core.bootstrap import bootstrap
    from roar.execution.runtime.coordinator import RunCoordinator
    from roar.execution.runtime.resources import build_host_runtime_resources

    bootstrap(ctx.roar_dir)

    runtime_resources = build_host_runtime_resources(ctx)
    coordinator = RunCoordinator(runtime_resources=runtime_resources)
    return coordinator.execute(ctx)


__all__ = [
    "ExecutionSetupError",
    "execute_host_run",
]
