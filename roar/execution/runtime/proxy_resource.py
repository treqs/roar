from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from roar.execution.cluster.proxy import ProxyService
from roar.execution.runtime.errors import ExecutionSetupError

from .resources import RuntimeObservationBundle, RuntimeResourceStart

if TYPE_CHECKING:
    from roar.core.interfaces.logger import ILogger
    from roar.core.models.run import RunContext
    from roar.execution.cluster.proxy import ProxyHandle


class ProxyRuntimeResource:
    """Runtime resource wrapper for per-run S3 proxy capture."""

    name = "proxy"

    def __init__(
        self,
        service: ProxyService | None = None,
        logger: ILogger | None = None,
    ) -> None:
        self._service = service or ProxyService()
        self._logger = logger
        self._handle: ProxyHandle | None = None

        if not self._service.find_proxy():
            raise ExecutionSetupError(
                "Error: S3 proxy is enabled but roar-proxy binary not found.\n"
                "Build it with: cargo build --release --manifest-path rust/Cargo.toml -p roar-proxy\n"
                "Or disable: roar proxy disable"
            )

    @property
    def logger(self) -> ILogger:
        if self._logger is None:
            from roar.core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    @property
    def active(self) -> bool:
        return self._handle is not None

    def start(self, ctx: RunContext, environ: Mapping[str, str]) -> RuntimeResourceStart:
        del ctx
        # Detect a pre-existing S3 endpoint (e.g. MinIO/LocalStack) to chain to.
        # Prefer the S3-scoped vars we also inject below, but fall back to the
        # generic AWS_ENDPOINT_URL — that is what most MinIO/LocalStack users
        # actually set, so ignoring it here would leave the proxy with no upstream
        # and drop their S3 traffic on the floor.
        existing_endpoint = (
            str(
                environ.get("AWS_ENDPOINT_URL_S3")
                or environ.get("S3_ENDPOINT_URL")
                or environ.get("AWS_ENDPOINT_URL")
                or ""
            ).strip()
            or None
        )
        try:
            self._handle = self._service.start_for_run(upstream_url=existing_endpoint)
        except Exception as exc:
            self._handle = None
            self.logger.warning("Failed to start proxy: %s", exc)
            return RuntimeResourceStart()

        proxy_url = f"http://127.0.0.1:{self._handle.port}"
        # Point S3 clients at the proxy with the S3-*scoped* endpoint override,
        # not the service-agnostic AWS_ENDPOINT_URL. AWS_ENDPOINT_URL_S3 is
        # honored by boto3 / aws CLI / aws-sdk-go-v2 and, unlike the global var,
        # does not redirect non-S3 services (STS, EC2, CloudWatch, ...) into the
        # S3-only proxy. S3_ENDPOINT_URL covers s5cmd, which ignores the SDK vars.
        env = {
            "AWS_ENDPOINT_URL_S3": proxy_url,
            "S3_ENDPOINT_URL": proxy_url,
        }
        if existing_endpoint:
            env["ROAR_UPSTREAM_S3_ENDPOINT"] = existing_endpoint

        self.logger.debug("Proxy runtime resource started on port %d", self._handle.port)
        return RuntimeResourceStart(env=env)

    def stop(self, *, exit_code: int | None) -> RuntimeObservationBundle:
        del exit_code
        if self._handle is None:
            return RuntimeObservationBundle()

        handle = self._handle
        self._handle = None
        entries = tuple(self._service.stop_for_run(handle))
        self.logger.debug("Proxy runtime resource stopped, collected %d S3 entries", len(entries))
        return RuntimeObservationBundle(s3_entries=entries)


__all__ = ["ProxyRuntimeResource"]
