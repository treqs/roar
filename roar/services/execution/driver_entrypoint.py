from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence

from roar.execution.framework.contract import (
    ROAR_EXECUTION_BACKEND_ENV,
)
from roar.execution.framework.registry import (
    get_execution_backend,
)
from roar.services.execution.fragment_transport import emit_fragment_dicts
from roar.services.execution.proxy import ProxyHandle, ProxyService, S3LogEntry
from roar.services.execution.proxy_config import local_proxy_port_from_env


def _warn(message: str) -> None:
    with contextlib.suppress(Exception):
        sys.stderr.write(message + "\n")


def _resolve_execution_backend(environ: Mapping[str, str]) -> str:
    return str(environ.get(ROAR_EXECUTION_BACKEND_ENV) or "").strip() or "ray"


def _build_driver_proxy_fragment(
    entries: Sequence[S3LogEntry],
    *,
    started_at: float,
    ended_at: float,
    exit_code: int,
    environ: Mapping[str, str],
) -> dict[str, object] | None:
    backend = get_execution_backend(_resolve_execution_backend(environ))
    return backend.driver_bootstrap.build_proxy_fragment(
        entries,
        started_at,
        ended_at,
        exit_code,
        environ,
    )


def _emit_driver_proxy_fragment(fragment: dict[str, object], *, environ: Mapping[str, str]) -> None:
    backend = get_execution_backend(_resolve_execution_backend(environ))
    emit_fragment_dicts(
        [fragment],
        env=environ,
        local_merge=backend.driver_bootstrap.local_merge,
    )


def _local_proxy_port(environ: Mapping[str, str]) -> int:
    return local_proxy_port_from_env(environ)


def _should_start_driver_proxy(environ: Mapping[str, str]) -> bool:
    backend = get_execution_backend(_resolve_execution_backend(environ))
    predicate = backend.driver_bootstrap.should_start_local_proxy
    if predicate is None:
        return True
    return bool(predicate(environ))


def _start_driver_proxy(
    environ: Mapping[str, str],
) -> tuple[ProxyService | None, ProxyHandle | None]:
    if not _should_start_driver_proxy(environ):
        return None, None

    service = ProxyService()
    handle = service.start_for_run(
        session_id=str(environ.get("ROAR_SESSION_ID", "")).strip() or None,
        job_id=str(environ.get("ROAR_JOB_ID", "")).strip() or None,
        upstream_url=str(environ.get("ROAR_UPSTREAM_S3_ENDPOINT", "")).strip() or None,
        port=_local_proxy_port(environ),
    )
    return service, handle


def _run_child(argv: Sequence[str], env: dict[str, str]) -> int:
    env.setdefault("ROAR_DRIVER_PHASE_CAPTURE", "1")
    process = subprocess.Popen(list(argv), env=env)
    return int(process.wait())


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        _warn("roar driver entrypoint requires a command after --")
        return 2

    service: ProxyService | None = None
    handle: ProxyHandle | None = None
    child_env = os.environ.copy()
    started_at = time.time()

    try:
        try:
            service, handle = _start_driver_proxy(os.environ)
        except Exception as exc:
            _warn(f"[roar-driver] failed to start local S3 proxy: {exc}")
        else:
            if handle is not None:
                child_env["ROAR_PROXY_PORT"] = str(handle.port)

        exit_code = _run_child(args, env=child_env)
    finally:
        ended_at = time.time()
        if service is not None and handle is not None:
            try:
                entries = service.stop_for_run(handle)
                fragment = _build_driver_proxy_fragment(
                    entries,
                    started_at=started_at,
                    ended_at=ended_at,
                    exit_code=locals().get("exit_code", 1),
                    environ=os.environ,
                )
                if fragment is not None:
                    _emit_driver_proxy_fragment(fragment, environ=os.environ)
            except Exception as exc:
                _warn(f"[roar-driver] failed to collect local S3 proxy lineage: {exc}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
