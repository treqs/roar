from __future__ import annotations

from roar.backends.ray.plugin import RAY_EXECUTION_BACKEND
from roar.execution.framework import iter_execution_backends, maybe_rewrite_submit_command


def test_canonical_execution_framework_imports_are_available() -> None:
    assert callable(maybe_rewrite_submit_command)
    assert RAY_EXECUTION_BACKEND.name == "ray"
    assert any(backend.name == "ray" for backend in iter_execution_backends())
