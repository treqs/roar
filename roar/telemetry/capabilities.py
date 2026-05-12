"""Allowlisted coarse capabilities for product telemetry."""

from __future__ import annotations

import importlib.util
import os
import platform
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .install import detect_installer

DEFAULT_TELEMETRY_ENDPOINT = "https://api.glaas.ai/api/v1/telemetry/roar"

_BOOL_FEATURE_KEYS = (
    "proxy_enabled",
    "ray_enabled",
    "osmo_enabled",
    "composites_run_enabled",
)
_ENDPOINT_KINDS = {"default_prod", "default_dev", "custom", "unset"}


def collect_platform(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Collect coarse, allowlisted platform information."""

    resolved_env = os.environ if environ is None else environ
    return {
        "os": _normalize_os(sys.platform),
        "arch": platform.machine() or "unknown",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "shell": _shell_name(resolved_env.get("SHELL")),
        "installer": detect_installer(resolved_env),
        "containerized": _detect_containerized(resolved_env),
    }


def collect_feature_capabilities(endpoint: str | None = None) -> dict[str, Any]:
    """Collect coarse feature capability values."""

    return {
        "glaas_endpoint_kind": classify_endpoint(endpoint),
        "proxy_enabled": False,
        "ray_enabled": importlib.util.find_spec("ray") is not None,
        "osmo_enabled": importlib.util.find_spec("osmo") is not None,
        "composites_run_enabled": True,
    }


def classify_endpoint(endpoint: str | None) -> str:
    text = str(endpoint or "").strip().rstrip("/")
    if not text:
        return "unset"
    if text == DEFAULT_TELEMETRY_ENDPOINT:
        return "default_prod"
    lowered = text.lower()
    if "localhost" in lowered or "127.0.0.1" in lowered or "dev" in lowered:
        return "default_dev"
    return "custom"


def normalize_feature_capabilities(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Filter and coerce capability values to the public schema."""

    if not values:
        return {}

    normalized: dict[str, Any] = {}
    endpoint_kind = values.get("glaas_endpoint_kind")
    if endpoint_kind in _ENDPOINT_KINDS:
        normalized["glaas_endpoint_kind"] = endpoint_kind

    for key in _BOOL_FEATURE_KEYS:
        if key in values:
            normalized[key] = bool(values[key])
    return normalized


def _normalize_os(value: str) -> str:
    lowered = value.lower()
    if lowered.startswith("linux"):
        return "linux"
    if lowered == "darwin":
        return "macos"
    if lowered.startswith(("win32", "cygwin", "msys")):
        return "windows"
    return lowered or "unknown"


def _shell_name(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return Path(text).name or "unknown"


def _detect_containerized(environ: Mapping[str, str]) -> bool:
    if Path("/.dockerenv").exists():
        return True
    container_value = str(environ.get("container") or "").strip()
    return bool(container_value)
