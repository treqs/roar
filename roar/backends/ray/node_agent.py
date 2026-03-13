from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import ray

from roar.backends.ray._agent_names import build_node_agent_name
from roar.execution.cluster.bridge import LocalProxyClusterBridge, SidecarHandle
from roar.execution.cluster.proxy_config import local_proxy_port_from_env

__all__ = ["RoarNodeAgent", "build_node_agent_name"]


def _local_proxy_port() -> int:
    return local_proxy_port_from_env(os.environ)


@ray.remote(num_cpus=0)
class RoarNodeAgent:
    def __init__(self, job_id: str) -> None:
        self._job_id = str(job_id)
        self._bridge = LocalProxyClusterBridge(
            Path(__file__).resolve().parents[2],
            message_sink=print,
        )
        self._proxy_handle: SidecarHandle | None = None
        self._proxy_port: int | None = None
        self._node_id = self._runtime_node_id()
        self._start_proxy()

    def _runtime_node_id(self) -> str | None:
        try:
            ctx = ray.get_runtime_context()
            value = ctx.get_node_id()
        except Exception:
            return None

        if value is None:
            return None
        if isinstance(value, bytes):
            return value.hex()

        to_hex = getattr(value, "hex", None)
        if callable(to_hex):
            try:
                return str(to_hex())
            except Exception:
                pass

        return str(value)

    def _start_proxy(self) -> None:
        upstream = str(os.environ.get("ROAR_UPSTREAM_S3_ENDPOINT", "")).strip() or None
        handle = self._bridge.start(
            job_id=self._job_id,
            port=_local_proxy_port(),
            upstream_url=upstream,
        )
        if handle is None:
            return
        self._proxy_handle = handle
        self._proxy_port = handle.port

    def _terminate_proxy(self) -> None:
        self._bridge.stop(self._proxy_handle)

    def get_proxy_port(self) -> int | None:
        return self._proxy_port

    def collect_logs(self) -> dict[str, Any]:
        return {
            "job_id": self._job_id,
            "node_id": self._node_id,
            "proxy_port": self._proxy_port,
            "proxy_log_lines": self._bridge.log_lines(self._proxy_handle),
        }

    def get_log_entries_since(self, since_index: int) -> dict[str, Any]:
        log_lines = self._bridge.log_lines(self._proxy_handle)
        new_lines = log_lines[since_index:]
        current_index = len(log_lines)
        return {
            "entries": new_lines,
            "current_index": current_index,
            "node_id": self._node_id,
            "proxy_port": self._proxy_port,
        }

    def shutdown(self) -> None:
        self._terminate_proxy()
