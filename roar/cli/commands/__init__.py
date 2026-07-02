"""
Click command implementations for roar CLI.

This package contains the Click-based command implementations that
replace the legacy command classes. Each module corresponds to a
roar command (e.g., run.py implements 'roar run').

Commands are lazily imported via the LazyCommand mechanism in roar.cli.
This __init__ defers all imports to avoid loading every command module
when only one command is invoked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import click

_COMMAND_MODULES: dict[str, tuple[str, str]] = {
    "auth": (".auth", "auth"),
    "build": (".build", "build"),
    "config": (".config", "config"),
    "dag": (".dag", "dag"),
    "env": (".env", "env"),
    "get": (".get", "get"),
    "init": (".init", "init"),
    "lineage": (".lineage", "lineage"),
    "log": (".log", "log"),
    "pop": (".pop", "pop"),
    "put": (".put", "put"),
    "register": (".register", "register"),
    "reproduce": (".reproduce", "reproduce"),
    "reset": (".reset", "reset"),
    "run": (".run", "run"),
    "show": (".show", "show"),
    "status": (".status", "status"),
    "tag": (".tag", "tag"),
    "tracer": (".tracer", "tracer"),
    "workflow": (".workflow", "workflow"),
}


def get_migrated_commands() -> list[click.Command]:
    """Import and return all command objects (used by legacy registration paths)."""
    import importlib

    commands = []
    for module_path, attr_name in _COMMAND_MODULES.values():
        mod = importlib.import_module(module_path, __name__)
        commands.append(getattr(mod, attr_name))
    return commands


def __getattr__(name: str):
    if name == "MIGRATED_COMMANDS":
        return get_migrated_commands()
    if name in _COMMAND_MODULES:
        import importlib

        module_path, attr_name = _COMMAND_MODULES[name]
        mod = importlib.import_module(module_path, __name__)
        value = getattr(mod, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "MIGRATED_COMMANDS",
    *_COMMAND_MODULES.keys(),
]
