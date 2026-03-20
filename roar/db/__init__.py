"""Lazy exports for the roar database layer."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "Artifact": ".models",
    "ArtifactHash": ".models",
    "Base": ".models",
    "Collection": ".models",
    "CollectionMember": ".models",
    "CompositeArtifactComponent": ".models",
    "CompositeMembershipIndex": ".models",
    "DatabaseContext": ".context",
    "HashCache": ".models",
    "Job": ".models",
    "JobInput": ".models",
    "JobOutput": ".models",
    "Label": ".models",
    "QueryDatabaseContext": ".query_context",
    "SchemaVersion": ".models",
    "Session": ".models",
    "create_database_context": ".context",
    "create_query_database_context": ".query_context",
    "create_roar_engine": ".engine",
    "create_session_factory": ".engine",
    "init_database": ".engine",
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
