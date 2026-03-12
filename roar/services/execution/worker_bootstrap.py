from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from typing import Any

from roar.services.execution.distributed_backends import (
    ROAR_EXECUTION_BACKEND_ENV,
    get_execution_backend,
)

WORKER_SETUP_HOOK = "roar.services.execution.worker_bootstrap.startup"
WORKER_PY_EXECUTABLE = "roar-worker"


def _resolve_execution_backend_name(environ: Mapping[str, str]) -> str:
    return str(environ.get(ROAR_EXECUTION_BACKEND_ENV) or "").strip() or "ray"


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
    working_dir_merger = merge_working_dir or _merge_working_dir
    suppress_tracking = (
        suppress_tracking_factory
        if suppress_tracking_factory is not None
        else contextlib.nullcontext
    )

    existing_working_dir = runtime_env_out.get("working_dir")
    if isinstance(existing_working_dir, str) and existing_working_dir.strip():
        if os.path.isdir(existing_working_dir):
            prepared_working_dir = tempfile.mkdtemp(prefix=f"roar-worker-env-{job_id[:8]}-")
            with suppress_tracking():
                working_dir_merger(existing_working_dir, prepared_working_dir)
        else:
            if warn is not None:
                warn(
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
            from roar.services.execution.tracer_backends import find_preload_library

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
    prepared = dict(
        backend.worker_bootstrap.prepare_runtime_env(
            runtime_env,
            job_id,
            source_environ,
        )
    )
    env_vars = dict(prepared.get("env_vars", {}) or {})
    env_vars[ROAR_EXECUTION_BACKEND_ENV] = backend.name
    prepared["env_vars"] = env_vars
    prepared["py_executable"] = backend.worker_bootstrap.py_executable
    prepared["worker_process_setup_hook"] = backend.worker_bootstrap.setup_hook
    return prepared


def startup() -> None:
    backend = get_execution_backend(_resolve_execution_backend_name(os.environ))
    backend.worker_bootstrap.startup()


def run_worker_entrypoint(argv: list[str] | None = None) -> None:
    backend = get_execution_backend(_resolve_execution_backend_name(os.environ))
    backend.worker_bootstrap.run_entrypoint(list(sys.argv[1:] if argv is None else argv))


def main() -> None:
    startup()
    run_worker_entrypoint()


def _merge_working_dir(source_dir: str, target_dir: str) -> None:
    for entry in os.listdir(source_dir):
        src = os.path.join(source_dir, entry)
        dst = os.path.join(target_dir, entry)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except Exception:
            continue
