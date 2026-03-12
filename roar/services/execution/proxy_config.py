"""Shared local proxy configuration helpers for distributed backends."""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_LOCAL_PROXY_PORT = 19191


def resolve_local_proxy_port(raw_value: str | None) -> int:
    text = str(raw_value or "").strip()
    if text.isdigit():
        port = int(text)
        if 1024 < port <= 65535:
            return port
    return DEFAULT_LOCAL_PROXY_PORT


def local_proxy_port_from_env(environ: Mapping[str, str]) -> int:
    return resolve_local_proxy_port(environ.get("ROAR_PROXY_PORT"))


def local_proxy_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"
