"""Lazy exports for presenter implementations."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ConsolePresenter": ".console",
    "DagDataBuilder": ".dag_data_builder",
    "DagRenderer": ".dag_renderer",
    "NullPresenter": ".null",
    "ShowRenderer": ".show_renderer",
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
