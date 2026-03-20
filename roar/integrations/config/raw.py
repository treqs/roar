"""Lightweight config access for performance-sensitive preview paths."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib

_DEFAULT_GLAAS_WEB_URL = "https://glaas.ai"
_DEFAULT_REGISTRATION_OMIT = {
    "enabled": True,
    "secrets": {"values": []},
    "env_vars": {
        "names": [
            "WANDB_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "DATABASE_URL",
            "AWS_SECRET_ACCESS_KEY",
        ]
    },
    "patterns": [],
    "allowlist": {"patterns": []},
}


def _infer_search_stop(start: Path) -> Path:
    """Infer an upward-search boundary without importing the full settings stack."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            cwd=start,
            text=True,
        ).strip()
        if out:
            return Path(out).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return start.resolve()


def find_raw_config_file(
    start_dir: str | os.PathLike[str] | None = None,
    stop_dir: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find a local roar config file without loading Pydantic settings."""
    start = Path(start_dir) if start_dir else Path.cwd()
    stop_path = Path(stop_dir).resolve() if stop_dir else _infer_search_stop(start)

    for parent in [start, *list(start.parents)]:
        config_path = parent / ".roar" / "config.toml"
        if config_path.exists():
            return config_path

        pyproject = parent / "pyproject.toml"
        if pyproject.exists():
            try:
                with open(pyproject, "rb") as handle:
                    data = tomllib.load(handle)
                if "tool" in data and "roar" in data["tool"]:
                    return pyproject
            except (tomllib.TOMLDecodeError, OSError):
                pass

        if parent.resolve() == stop_path:
            break

    return None


def get_raw_glaas_web_url(
    start_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    """Resolve the GLaaS web URL for local preview output."""
    env_value = _normalize_url(os.environ.get("ROAR_GLAAS__WEB_URL"))
    if env_value is not None:
        return env_value

    raw_config = _load_raw_config(start_dir=start_dir)
    glaas_config = raw_config.get("glaas")
    if isinstance(glaas_config, dict):
        return _normalize_url(glaas_config.get("web_url"))
    return None


def get_raw_registration_omit_config(
    start_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve preview-only omit config without importing the full config package."""
    resolved = deepcopy(_DEFAULT_REGISTRATION_OMIT)
    raw_config = _load_raw_config(start_dir=start_dir)
    registration = raw_config.get("registration")
    if isinstance(registration, dict):
        omit_config = registration.get("omit")
        if isinstance(omit_config, dict):
            _deep_update(resolved, omit_config)

    _apply_registration_omit_env_overrides(resolved)
    return resolved


def _load_raw_config(
    *,
    start_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    path = find_raw_config_file(start_dir=start_dir)
    if path is None:
        return {}

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}

    if path.name == "pyproject.toml":
        tool_config = data.get("tool", {})
        if isinstance(tool_config, dict):
            roar_config = tool_config.get("roar", {})
            return roar_config if isinstance(roar_config, dict) else {}
        return {}

    return data if isinstance(data, dict) else {}


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_update(base[key], value)
            continue
        base[key] = value


def _apply_registration_omit_env_overrides(config: dict[str, Any]) -> None:
    enabled = os.environ.get("ROAR_REGISTRATION__OMIT__ENABLED")
    if enabled is not None:
        config["enabled"] = _parse_bool(enabled)

    secrets_values = os.environ.get("ROAR_REGISTRATION__OMIT__SECRETS__VALUES")
    if secrets_values is not None:
        config.setdefault("secrets", {})["values"] = _parse_list(secrets_values)

    env_var_names = os.environ.get("ROAR_REGISTRATION__OMIT__ENV_VARS__NAMES")
    if env_var_names is not None:
        config.setdefault("env_vars", {})["names"] = _parse_list(env_var_names)

    allowlist_patterns = os.environ.get("ROAR_REGISTRATION__OMIT__ALLOWLIST__PATTERNS")
    if allowlist_patterns is not None:
        config.setdefault("allowlist", {})["patterns"] = _parse_list(allowlist_patterns)

    custom_patterns = os.environ.get("ROAR_REGISTRATION__OMIT__PATTERNS")
    if custom_patterns is not None:
        parsed = _parse_json(custom_patterns)
        config["patterns"] = parsed if isinstance(parsed, list) else []


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _parse_list(value: str) -> list[str]:
    parsed = _parse_json(value)
    if isinstance(parsed, list):
        return [str(item) for item in parsed]

    stripped = value.strip()
    if not stripped:
        return []
    return [item.strip() for item in stripped.split(",") if item.strip()]


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _normalize_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped.rstrip("/")
