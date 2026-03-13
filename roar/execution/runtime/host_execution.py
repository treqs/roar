from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from roar.core.models.run import RunContext, RunResult


class ExecutionSetupError(RuntimeError):
    """Raised when a backend cannot start a host-side execution path."""


def execute_host_run(ctx: RunContext) -> RunResult:
    from roar.config import config_get
    from roar.core.bootstrap import bootstrap
    from roar.services.execution.coordinator import RunCoordinator

    bootstrap(ctx.roar_dir)

    proxy_service = None
    if config_get("proxy.enabled", start_dir=ctx.repo_root):
        from roar.execution.cluster.proxy import ProxyService

        proxy_service = ProxyService()
        if not proxy_service.find_proxy():
            raise ExecutionSetupError(
                "Error: S3 proxy is enabled but roar-proxy binary not found.\n"
                "Build it with: cargo build --release --manifest-path rust/Cargo.toml -p roar-proxy\n"
                "Or disable: roar proxy disable"
            )

    coordinator = RunCoordinator(proxy_service=proxy_service)
    return coordinator.execute(ctx)


__all__ = [
    "ExecutionSetupError",
    "execute_host_run",
]
