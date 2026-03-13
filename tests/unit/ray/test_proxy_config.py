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


def test_local_proxy_port_from_env_reads_roar_proxy_port() -> None:
    assert local_proxy_port_from_env({"ROAR_PROXY_PORT": "31234"}) == 31234


def test_local_proxy_endpoint_formats_loopback_url() -> None:
    assert local_proxy_endpoint(31234) == "http://127.0.0.1:31234"
