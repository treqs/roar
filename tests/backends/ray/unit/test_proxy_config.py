from __future__ import annotations

from roar.execution.cluster.proxy_config import (
    DEFAULT_LOCAL_PROXY_PORT,
    local_proxy_endpoint,
    local_proxy_port_from_env,
    resolve_local_proxy_port,
)


def test_resolve_local_proxy_port_accepts_valid_env_value() -> None:
    assert resolve_local_proxy_port("24567") == 24567


def test_resolve_local_proxy_port_falls_back_for_invalid_value() -> None:
    assert resolve_local_proxy_port("invalid") == DEFAULT_LOCAL_PROXY_PORT


