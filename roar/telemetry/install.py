"""Coarse install metadata for telemetry payloads."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib import metadata as importlib_metadata

UNKNOWN_VERSION = "0+unknown"


def get_roar_version() -> str:
    """Return the installed roar package version without importing package internals."""

    try:
        return importlib_metadata.version("roar-cli")
    except importlib_metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


def detect_installer(environ: Mapping[str, str] | None = None) -> str:
    """Return a coarse installer class without serializing paths."""

    resolved_env = os.environ if environ is None else environ
    if _truthy(resolved_env.get("PIPX_HOME")) or _truthy(resolved_env.get("PIPX_BIN_DIR")):
        return "pipx"
    if _truthy(resolved_env.get("UV_TOOL_DIR")) or _truthy(
        resolved_env.get("UV_PROJECT_ENVIRONMENT")
    ):
        return "uv-tool"
    if _truthy(resolved_env.get("CONDA_PREFIX")):
        return "conda"
    if _truthy(resolved_env.get("POETRY_ACTIVE")):
        return "poetry"
    if _truthy(resolved_env.get("PIP_REQ_TRACKER")):
        return "pip"
    return "unknown"


def _truthy(value: object) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and text not in {"0", "false", "no", "off"}
