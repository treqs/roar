"""Execution services for roar run/build commands."""

from importlib import import_module
from typing import Any

__all__ = [
    "DAGReferenceResolver",
    "ProcessSignalHandler",
    "ProxyService",
    "RunArgumentParser",
    "RunCoordinator",
    "TracerService",
]

_LAZY_IMPORTS = {
    "DAGReferenceResolver": (".dag_resolver", "DAGReferenceResolver"),
    "ProcessSignalHandler": (".signal_handler", "ProcessSignalHandler"),
    "ProxyService": (".proxy", "ProxyService"),
    "RunArgumentParser": (".args", "RunArgumentParser"),
    "RunCoordinator": (".coordinator", "RunCoordinator"),
    "TracerService": (".tracer", "TracerService"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _LAZY_IMPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
