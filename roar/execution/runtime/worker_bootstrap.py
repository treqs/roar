from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from typing import Any

from roar.execution.framework.contract import ROAR_EXECUTION_BACKEND_ENV
from roar.execution.framework.registry import get_execution_backend
from roar.execution.runtime.inject.support import SuppressTracking, warn_runtime
from roar.execution.runtime.inject.support import (
    merge_working_dir as default_merge_working_dir,
)

WORKER_SETUP_HOOK = "roar.execution.runtime.worker_bootstrap.startup"
WORKER_PY_EXECUTABLE = "roar-worker"
USER_SETUP_HOOK_ENV = "ROAR_USER_SETUP_HOOK"


def _resolve_execution_backend_name(environ: Mapping[str, str]) -> str:
    backend_name = str(environ.get(ROAR_EXECUTION_BACKEND_ENV) or "").strip()
    if backend_name:
        return backend_name
    raise RuntimeError(f"{ROAR_EXECUTION_BACKEND_ENV} is required for worker bootstrap")


def build_packaged_worker_runtime_env(
    runtime_env: Mapping[str, Any] | None,
    job_id: str,
    *,
    merge_working_dir: Callable[[str, str], None] | None = None,
    warn: Callable[[str, object], None] | None = None,
    suppress_tracking_factory: Callable[[], contextlib.AbstractContextManager[object]]
    | None = None,
) -> dict[str, Any]:
    runtime_env_out = dict(runtime_env or {})
    prepared_working_dir: str | None = None
    working_dir_merger = merge_working_dir or default_merge_working_dir
    suppress_tracking = (
        suppress_tracking_factory if suppress_tracking_factory is not None else SuppressTracking
    )
    warn_user = warn or warn_runtime

    existing_working_dir = runtime_env_out.get("working_dir")
    if isinstance(existing_working_dir, str) and existing_working_dir.strip():
        if os.path.isdir(existing_working_dir):
            prepared_working_dir = tempfile.mkdtemp(prefix=f"roar-worker-env-{job_id[:8]}-")
            with suppress_tracking():
                working_dir_merger(existing_working_dir, prepared_working_dir)
        else:
            if warn_user is not None:
                warn_user(
                    "Skipping working_dir merge for non-local path %s while preparing Ray worker wrapper",
                    existing_working_dir,
                )
            prepared_working_dir = existing_working_dir

    if not prepared_working_dir:
        prepared_working_dir = tempfile.mkdtemp(prefix=f"roar-worker-env-{job_id[:8]}-")

    if os.path.isdir(prepared_working_dir):
        try:
            from pathlib import Path

            import roar
            from roar.execution.runtime.tracer_backends import find_preload_library

            roar_package_dir = Path(roar.__file__).resolve().parent
            with suppress_tracking():
                shutil.copytree(
                    roar_package_dir,
                    os.path.join(prepared_working_dir, "roar"),
                    dirs_exist_ok=True,
                )

                preload_library = find_preload_library(roar_package_dir)
                if preload_library:
                    shutil.copy2(
                        preload_library,
                        os.path.join(prepared_working_dir, "libroar_tracer_preload.so"),
                    )
        except Exception:
            pass

    runtime_env_out["working_dir"] = prepared_working_dir
    return runtime_env_out


def prepare_worker_runtime_env(
    backend_name: str,
    runtime_env: Mapping[str, Any] | None,
    job_id: str,
    *,
    source_environ: Mapping[str, str],
) -> dict[str, Any]:
    backend = get_execution_backend(backend_name)
    distributed = backend.distributed
    if distributed is None:
        raise RuntimeError(f"execution backend {backend.name!r} does not support worker bootstrap")
    prepared = dict(
        distributed.worker_bootstrap.prepare_runtime_env(
            runtime_env,
            job_id,
            source_environ,
        )
    )
    env_vars = dict(prepared.get("env_vars", {}) or {})
    env_vars[ROAR_EXECUTION_BACKEND_ENV] = backend.name
    prepared["env_vars"] = env_vars
    prepared["py_executable"] = distributed.worker_bootstrap.py_executable
    prepared["worker_process_setup_hook"] = distributed.worker_bootstrap.setup_hook
    return prepared


def startup() -> None:
    backend = get_execution_backend(_resolve_execution_backend_name(os.environ))
    distributed = backend.distributed
    if distributed is None:
        raise RuntimeError(f"execution backend {backend.name!r} does not support worker bootstrap")
    distributed.worker_bootstrap.startup()
    _run_user_setup_hook(os.environ)


def _run_user_setup_hook(environ: Mapping[str, str]) -> None:
    """Chain a user worker_process_setup_hook displaced by roar's.

    Instrumented submits (RayJob rewrite) replace the user's setup hook
    with roar's and carry the original as ROAR_USER_SETUP_HOOK. User-hook
    failures propagate: without roar that failure would have failed the
    worker, and instrumentation must not change that contract.
    """
    hook_path = str(environ.get(USER_SETUP_HOOK_ENV) or "").strip()
    if not hook_path or hook_path == WORKER_SETUP_HOOK:
        return
    module_name, separator, attribute = hook_path.rpartition(".")
    if not separator:
        raise RuntimeError(f"{USER_SETUP_HOOK_ENV}={hook_path!r} is not a module.attribute path")
    import importlib

    hook = getattr(importlib.import_module(module_name), attribute)
    hook()


def run_worker_entrypoint(argv: list[str] | None = None) -> None:
    backend = get_execution_backend(_resolve_execution_backend_name(os.environ))
    distributed = backend.distributed
    if distributed is None:
        raise RuntimeError(f"execution backend {backend.name!r} does not support worker bootstrap")
    distributed.worker_bootstrap.run_entrypoint(list(sys.argv[1:] if argv is None else argv))


def main() -> None:
    startup()
    run_worker_entrypoint()
