"""Shared tracer backend mode constants and typing."""

from __future__ import annotations

from typing import Literal, TypeAlias

TracerMode: TypeAlias = Literal["auto", "ebpf", "preload", "ptrace"]
TracerBackend: TypeAlias = Literal["ebpf", "preload", "ptrace"]

TRACER_MODE_VALUES: tuple[TracerMode, ...] = ("auto", "ebpf", "preload", "ptrace")
TRACER_BACKEND_ORDER: tuple[TracerBackend, ...] = ("ebpf", "preload", "ptrace")

VALID_TRACER_MODES: frozenset[str] = frozenset(TRACER_MODE_VALUES)


def is_valid_tracer_mode(value: str) -> bool:
    """Return True when value is a supported tracer mode."""
    return value in VALID_TRACER_MODES
