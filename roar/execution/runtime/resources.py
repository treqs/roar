from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from roar.execution.cluster.proxy import S3LogEntry

if TYPE_CHECKING:
    from roar.core.interfaces.logger import ILogger
    from roar.core.models.run import RunContext


@dataclass(frozen=True)
class RuntimeResourceStart:
    """Result of starting a host runtime resource."""

    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeObservationBundle:
    """Observations collected from host runtime resources."""

    s3_entries: tuple[S3LogEntry, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


class HostRuntimeResource(Protocol):
    """Explicit start/stop contract for host runtime resources."""

    name: str

    def start(self, ctx: RunContext, environ: Mapping[str, str]) -> RuntimeResourceStart: ...

    def stop(self, *, exit_code: int | None) -> RuntimeObservationBundle: ...

    @property
    def active(self) -> bool: ...


class RuntimeResourceController:
    """Owns deterministic startup and teardown of host runtime resources."""

    def __init__(
        self,
        resources: Sequence[HostRuntimeResource] | None = None,
        logger: ILogger | None = None,
    ) -> None:
        self._resources = tuple(resources or ())
        self._started_resources: list[HostRuntimeResource] = []
        self._stopped = False
        self._cached_stop_result = RuntimeObservationBundle()
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        if self._logger is None:
            from roar.core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def start_all(self, ctx: RunContext, environ: Mapping[str, str]) -> dict[str, str]:
        """Start all configured resources and return merged env patches."""
        merged_env: dict[str, str] = {}
        self._started_resources = []
        self._stopped = False
        self._cached_stop_result = RuntimeObservationBundle()

        for resource in self._resources:
            effective_environ = dict(environ)
            effective_environ.update(merged_env)
            result = resource.start(ctx, effective_environ)
            merged_env.update({str(key): str(value) for key, value in result.env.items()})
            self._started_resources.append(resource)

        return merged_env

    def stop_all(self, *, exit_code: int | None) -> RuntimeObservationBundle:
        """Stop resources exactly once and return aggregated observations."""
        if self._stopped:
            return self._cached_stop_result

        collected_s3_entries: list[S3LogEntry] = []
        merged_metadata: dict[str, Any] = {}

        for resource in reversed(self._started_resources):
            try:
                result = resource.stop(exit_code=exit_code)
            except Exception as exc:
                self.logger.warning(
                    "Failed to stop runtime resource %s cleanly: %s",
                    resource.name,
                    exc,
                )
                continue

            collected_s3_entries.extend(result.s3_entries)
            merged_metadata.update(dict(result.metadata))

        self._cached_stop_result = RuntimeObservationBundle(
            s3_entries=tuple(collected_s3_entries),
            metadata=merged_metadata,
        )
        self._stopped = True
        return self._cached_stop_result

    def active_resource_names(self) -> tuple[str, ...]:
        """Return active resource names in start order."""
        return tuple(resource.name for resource in self._started_resources if resource.active)


def build_host_runtime_resources(ctx: RunContext) -> tuple[HostRuntimeResource, ...]:
    """Build the host runtime resources required for a tracked host execution."""
    from roar.integrations.config import config_get

    resources: list[HostRuntimeResource] = []
    if config_get("proxy.enabled", start_dir=ctx.repo_root):
        from .proxy_resource import ProxyRuntimeResource

        resources.append(ProxyRuntimeResource())

    return tuple(resources)


__all__ = [
    "HostRuntimeResource",
    "RuntimeObservationBundle",
    "RuntimeResourceController",
    "RuntimeResourceStart",
    "build_host_runtime_resources",
]
