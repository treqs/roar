"""Lazy exports for roar configuration helpers."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AnalyzersConfig": ".schema",
    "CleanupConfig": ".schema",
    "CONFIGURABLE_KEYS": ".access",
    "CORE_CONFIGURABLE_KEYS": ".access",
    "CompositesConfig": ".schema",
    "ConfigBaseModel": ".schema",
    "FiltersConfig": ".schema",
    "GlaasConfig": ".schema",
    "HashConfig": ".schema",
    "LoggingConfig": ".schema",
    "OutputConfig": ".schema",
    "ProxyConfig": ".schema",
    "RegisterConfig": ".schema",
    "ReversibleConfig": ".schema",
    "RoarConfig": ".schema",
    "RoarSettings": ".loader",
    "RunCompositeConfig": ".schema",
    "TracerConfig": ".schema",
    "VALID_HASH_ALGORITHMS": ".access",
    "_get_default_config": ".access",
    "config_get": ".access",
    "config_list": ".access",
    "config_set": ".access",
    "find_config_file": ".loader",
    "find_raw_config_file": ".raw",
    "find_roar_dir": ".loader",
    "get_config_path_for_write": ".access",
    "get_configurable_keys": ".access",
    "get_hash_algorithms": ".access",
    "get_raw_glaas_web_url": ".raw",
    "get_raw_registration_omit_config": ".raw",
    "get_roar_dir": ".access",
    "load_config": ".access",
    "load_settings": ".loader",
    "save_config": ".access",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
