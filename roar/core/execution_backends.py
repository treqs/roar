"""Shared execution backend mode constants and typing."""

from __future__ import annotations

from typing import Literal, TypeAlias

ExecutionBackend: TypeAlias = Literal["local", "ray"]

EXECUTION_BACKEND_VALUES: tuple[ExecutionBackend, ...] = ("local", "ray")
VALID_EXECUTION_BACKENDS: frozenset[str] = frozenset(EXECUTION_BACKEND_VALUES)


def is_valid_execution_backend(value: str) -> bool:
    """Return True when value is a supported execution backend."""
    return value in VALID_EXECUTION_BACKENDS
