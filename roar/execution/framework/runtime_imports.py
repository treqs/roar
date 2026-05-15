"""Framework-owned runtime import dispatch for instrumented processes."""

from __future__ import annotations

import contextlib
from collections.abc import MutableMapping
from typing import Any

from roar.execution.framework.contract import (
    ROAR_EXECUTION_BACKEND_ENV,
    ExecutionBackend,
)
from roar.execution.framework.registry import (
    get_execution_backend,
    match_execution_backend_for_module,
)


class RuntimeImportController:
    """Coordinate backend runtime import lifecycle for an instrumented process."""

    def __init__(self, environ: MutableMapping[str, str]):
        self._environ = environ
        self._initialized_backend_names: set[str] = set()
        self._backend_dispatch_disabled = False

    def disable_backend_dispatch(self) -> None:
        """Suppress backend init / observe / patch for the lifetime of this controller.

        Used when the traced Python's ABI doesn't match roar's bundled compiled
        deps — loading a backend plugin would trigger an ``ImportError`` from
        wrong-ABI wheels (pydantic_core, etc.). The stdlib tracker hooks
        (file opens, environ reads, import names) keep running.
        """
        self._backend_dispatch_disabled = True

    def resolve_selected_backend(self) -> ExecutionBackend | None:
        backend_name = str(self._environ.get(ROAR_EXECUTION_BACKEND_ENV) or "").strip()
        if not backend_name:
            return None
        try:
            return get_execution_backend(backend_name)
        except Exception:
            return None

    def initialize_selected_backend(self) -> None:
        if self._backend_dispatch_disabled:
            return
        backend = self.resolve_selected_backend()
        if backend is not None:
            self._initialize_backend(backend)

    def handle_import(self, module_name: str, module: Any) -> ExecutionBackend | None:
        if self._backend_dispatch_disabled:
            return None

        matched_backend = match_execution_backend_for_module(module_name)
        if matched_backend is not None:
            self._environ.setdefault(ROAR_EXECUTION_BACKEND_ENV, matched_backend.name)

        selected_backend = self.resolve_selected_backend()
        if selected_backend is not None:
            self._initialize_backend(selected_backend)
            self._observe_import(selected_backend, module_name, module)

        if matched_backend is not None:
            self._initialize_backend(matched_backend)
            self._patch_module(matched_backend, module_name, module)

        return matched_backend

    def _initialize_backend(self, backend: ExecutionBackend) -> None:
        distributed = backend.distributed
        adapter = distributed.runtime_import if distributed is not None else None
        if adapter is None or backend.name in self._initialized_backend_names:
            return
        initializer = adapter.initialize_process
        if initializer is None:
            self._initialized_backend_names.add(backend.name)
            return
        try:
            initializer()
        except Exception:
            return
        self._initialized_backend_names.add(backend.name)

    @staticmethod
    def _observe_import(backend: ExecutionBackend, module_name: str, module: Any) -> None:
        distributed = backend.distributed
        adapter = distributed.runtime_import if distributed is not None else None
        if adapter is None or adapter.observe_import is None:
            return
        with contextlib.suppress(Exception):
            adapter.observe_import(module_name, module)

    @staticmethod
    def _patch_module(backend: ExecutionBackend, module_name: str, module: Any) -> None:
        distributed = backend.distributed
        adapter = distributed.runtime_import if distributed is not None else None
        if adapter is None or adapter.patch_module is None:
            return
        with contextlib.suppress(Exception):
            adapter.patch_module(module_name, module)


__all__ = ["RuntimeImportController"]
