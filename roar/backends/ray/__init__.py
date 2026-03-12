"""Canonical Ray backend imports."""

from roar.backends.ray.plugin import RAY_EXECUTION_BACKEND, register

__all__ = ["RAY_EXECUTION_BACKEND", "register"]
