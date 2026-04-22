"""Interactive terminal UI for roar."""

from __future__ import annotations

__all__ = ["run"]


def run(*args, **kwargs):  # pragma: no cover - thin shim
    from .app import run as _run

    return _run(*args, **kwargs)
