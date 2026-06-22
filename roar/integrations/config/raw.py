"""Lightweight config access for performance-sensitive preview paths."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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


def _config_search_start(
    start_dir: str | os.PathLike[str] | None,
    local_config_path: Path | None,
) -> Path:
    if start_dir is not None:
        return Path(start_dir)
    if local_config_path and local_config_path.name == "config.toml":
        return local_config_path.parent.parent
    if local_config_path:
        return local_config_path.parent
    return Path.cwd()


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
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the GLaaS web URL for local preview output.

    Precedence: ``ROAR_GLAAS__WEB_URL`` env, explicit ``glaas.web_url`` config,
    then a value derived from ``glaas.url`` (so a dev/staging API URL produces
    matching dev/staging web links instead of falling back to prod).
    """
    resolved_env = os.environ if environ is None else environ
    env_value = _normalize_url(resolved_env.get("ROAR_GLAAS__WEB_URL"))
    if env_value is not None:
        return env_value

    raw_config = load_raw_config(start_dir=start_dir)
    glaas_config = raw_config.get("glaas")
    if isinstance(glaas_config, dict):
        explicit = _normalize_url(glaas_config.get("web_url"))
        if explicit is not None:
            return explicit

    return _derive_web_url_from_api(
        get_raw_glaas_api_url(start_dir=start_dir, environ=resolved_env)
    )


def _derive_web_url_from_api(api_url: str | None) -> str | None:
    """Derive the web UI URL from the API URL by dropping a leading ``api.`` host label.

    ``https://api.glaas.ai``     -> ``https://glaas.ai``
    ``https://api.dev.glaas.ai`` -> ``https://dev.glaas.ai``

    Hosts without an ``api.`` prefix (e.g. ``http://localhost:3000``) can't be
    transformed reliably, so this returns ``None`` and the caller falls back.
    """
    if not api_url:
        return None
    parts = urlsplit(api_url)
    host = parts.hostname
    if not host or not host.startswith("api."):
        return None
    netloc = host[len("api.") :]
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, "", "", "")).rstrip("/")


def get_raw_glaas_api_url(
    start_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the GLaaS API URL without importing the full settings stack."""
    resolved_env = os.environ if environ is None else environ
    env_value = _normalize_url(resolved_env.get("ROAR_GLAAS__URL"))
    if env_value is not None:
        return env_value

    legacy_env_value = _normalize_url(resolved_env.get("GLAAS_URL"))
    if legacy_env_value is not None:
        return legacy_env_value

    raw_config = load_raw_config(start_dir=start_dir)
    glaas_config = raw_config.get("glaas")
    if isinstance(glaas_config, dict):
        return _normalize_url(glaas_config.get("url"))
    return None


def get_raw_registration_omit_config(
    start_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Resolve preview-only omit config without importing the full config package."""
    resolved = deepcopy(_DEFAULT_REGISTRATION_OMIT)
    raw_config = load_raw_config(start_dir=start_dir)
    registration = raw_config.get("registration")
    if isinstance(registration, dict):
        omit_config = registration.get("omit")
        if isinstance(omit_config, dict):
            _deep_update(resolved, omit_config)

    _apply_registration_omit_env_overrides(resolved)
    return resolved


def load_raw_config(
    *,
    start_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load the nearest raw roar config without applying model defaults."""
    return _load_raw_config(start_dir=start_dir)


def _load_raw_config(
    *,
    start_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    path = find_raw_config_file(start_dir=start_dir)
    config: dict[str, Any] = {}

    roarconfig = _infer_search_stop(_config_search_start(start_dir, path)) / ".roarconfig"
    if roarconfig.exists():
        roarconfig_data = _load_raw_toml_file(roarconfig)
        if isinstance(roarconfig_data, dict):
            config = roarconfig_data

    if path is None:
        return config

    local_config = _load_raw_toml_file(path)
    if path.name == "pyproject.toml":
        tool_config = local_config.get("tool", {}) if isinstance(local_config, dict) else {}
        if isinstance(tool_config, dict):
            roar_config = tool_config.get("roar", {})
            local_config = roar_config if isinstance(roar_config, dict) else {}
        else:
            local_config = {}

    if isinstance(local_config, dict):
        _deep_update(config, local_config)
    return config


def _load_raw_toml_file(path: Path) -> dict[str, Any]:
    """Load a TOML file as a plain dict, returning empty on parse/read failure."""

    try:
        with open(path, "rb") as handle:
            loaded = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        return {}

    return loaded if isinstance(loaded, dict) else {}


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
