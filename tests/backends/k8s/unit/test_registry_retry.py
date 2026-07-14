from __future__ import annotations

from roar.execution.framework import registry


def test_lookup_retries_transiently_skipped_builtin_imports() -> None:
    """A backend whose plugin import failed early must recover on lookup.

    Sitecustomize (ROAR_WRAP=1) can trigger discovery at interpreter startup
    before the runtime env's site-packages are importable (observed in Ray
    pip virtualenvs); the failure must not poison the registry for the
    lifetime of the process.
    """
    registry._ensure_execution_backends_discovered()
    saved_backends = list(registry._registered_execution_backends)
    saved_skipped = dict(registry._skipped_builtin_backend_imports)
    try:
        # Simulate the poisoned state: the k8s plugin import "failed" early.
        registry._registered_execution_backends[:] = [
            backend for backend in saved_backends if backend.name != "k8s"
        ]
        registry._skipped_builtin_backend_imports.clear()
        registry._skipped_builtin_backend_imports["roar.backends.k8s.plugin"] = (
            "No module named 'cryptography'"
        )

        backend = registry.get_execution_backend("k8s")
        assert backend.name == "k8s"
        assert "roar.backends.k8s.plugin" not in registry._skipped_builtin_backend_imports
    finally:
        registry._registered_execution_backends[:] = saved_backends
        registry._skipped_builtin_backend_imports.clear()
        registry._skipped_builtin_backend_imports.update(saved_skipped)
