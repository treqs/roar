from __future__ import annotations

import sys
import textwrap

import pytest

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


def test_retry_is_not_reentrant_from_hooked_plugin_imports(tmp_path, monkeypatch) -> None:
    """Lookups fired from inside the retry's own plugin imports must not re-enter it.

    The tracking import hook routes every import — including the retry's own
    plugin imports — through handle_import, whose backend resolution lands
    back in get_execution_backend. Re-entering the retry there fans each
    hooked import out into another full retry pass: unbounded mutual
    recursion that pins the CPU and keeps Ray workers from registering
    (observed at worker startup on the K8s/Ray dogfood cluster).
    """
    module_name = "roar_test_reentrant_plugin"
    (tmp_path / f"{module_name}.py").write_text(
        textwrap.dedent(
            """
            from roar.execution.framework import registry

            # Mimic the tracking hook resolving the selected backend while
            # this plugin module is being imported by the retry itself.
            try:
                registry.get_execution_backend("still-absent-backend")
            except LookupError:
                pass


            def register():
                return None
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(module_name, None)

    registry._ensure_execution_backends_discovered()
    saved_skipped = dict(registry._skipped_builtin_backend_imports)

    retry_calls = 0
    real_retry = registry._retry_skipped_builtin_backend_imports

    def counting_retry() -> None:
        nonlocal retry_calls
        retry_calls += 1
        real_retry()

    monkeypatch.setattr(registry, "_retry_skipped_builtin_backend_imports", counting_retry)
    monkeypatch.setattr(
        registry,
        "_BUILTIN_EXECUTION_BACKEND_MODULES",
        (*registry._BUILTIN_EXECUTION_BACKEND_MODULES, module_name),
    )
    registry._skipped_builtin_backend_imports.clear()
    # Two entries so a re-entrant retry pass would have real work to do.
    registry._skipped_builtin_backend_imports[module_name] = "transient"
    registry._skipped_builtin_backend_imports["roar_test_never_importable"] = "transient"

    try:
        with pytest.raises(LookupError):
            registry.get_execution_backend("absent-backend")
        assert retry_calls == 1, "nested lookup during plugin import re-entered the retry"
        # The importable plugin registered and left the skipped list.
        assert module_name not in registry._skipped_builtin_backend_imports
    finally:
        sys.modules.pop(module_name, None)
        registry._skipped_builtin_backend_imports.clear()
        registry._skipped_builtin_backend_imports.update(saved_skipped)
