"""Backend resolution helpers shared by transfer commands."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any


def load_backend_class(
    module_path: str,
    class_name: str,
    dependency_error_message: str,
) -> type[Any]:
    """Load backend class, raising an ImportError with a user-facing message."""
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(dependency_error_message) from e
    return getattr(module, class_name)


def resolve_backend_for_scheme(
    scheme: str,
    builders: dict[str, Callable[[], Any]],
    unsupported_message: str,
) -> Any:
    """Resolve backend instance using a scheme->builder map."""
    builder = builders.get(scheme)
    if builder is None:
        raise ValueError(unsupported_message)
    return builder()
