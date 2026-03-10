from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Sequence

from roar.ray.collector import collect_fragments
from roar.ray.fragment import TaskFragment
from roar.ray.glaas_fragment_streamer import GlaasFragmentStreamer
from roar.ray.proxy_fragments import build_proxy_fragment
from roar.services.execution.proxy import ProxyHandle, ProxyService, S3LogEntry

_DEFAULT_LOCAL_PROXY_PORT = 19191


def _warn(message: str) -> None:
    with contextlib.suppress(Exception):
        sys.stderr.write(message + "\n")


def _build_driver_proxy_fragment(
    entries: Sequence[S3LogEntry],
    *,
    started_at: float,
    ended_at: float,
    exit_code: int,
) -> object | None:
    return build_proxy_fragment(
        entries,
        function_name="s3_driver_proxy",
        task_id="proxy:driver",
        parent_job_uid=os.environ.get("ROAR_JOB_ID"),
        started_at=started_at,
        ended_at=ended_at,
        exit_code=exit_code,
    )


def _emit_driver_proxy_fragment(fragment: TaskFragment) -> None:
    session_id = str(os.environ.get("ROAR_SESSION_ID", "")).strip()
    token = str(os.environ.get("ROAR_FRAGMENT_TOKEN", "")).strip()
    glaas_url = str(os.environ.get("GLAAS_URL") or "").strip()

    if session_id and token and glaas_url:
        streamer = GlaasFragmentStreamer(
            session_id=session_id,
            token=token,
            glaas_url=glaas_url,
        )
        streamer.append_fragment(fragment.to_dict())
        streamer.close()
        return

    project_dir = str(os.environ.get("ROAR_PROJECT_DIR", "")).strip()
    if not project_dir:
        return

    collect_fragments(
        fragments=[fragment.to_dict()],
        project_dir=project_dir,
        driver_job_uid=os.environ.get("ROAR_JOB_ID"),
    )


def _local_proxy_port() -> int:
    raw_value = str(os.environ.get("ROAR_PROXY_PORT", "")).strip()
    if raw_value.isdigit():
        port = int(raw_value)
        if 1024 < port <= 65535:
            return port
    return _DEFAULT_LOCAL_PROXY_PORT


def _start_driver_proxy() -> tuple[ProxyService | None, ProxyHandle | None]:
    endpoint = str(os.environ.get("AWS_ENDPOINT_URL", "")).strip().lower()
    if endpoint and not (
        endpoint.startswith("http://127.0.0.1:") or endpoint.startswith("http://localhost:")
    ):
        return None, None

    service = ProxyService()
    handle = service.start_for_run(
        session_id=str(os.environ.get("ROAR_SESSION_ID", "")).strip() or None,
        job_id=str(os.environ.get("ROAR_JOB_ID", "")).strip() or None,
        upstream_url=str(os.environ.get("ROAR_UPSTREAM_S3_ENDPOINT", "")).strip() or None,
        port=_local_proxy_port(),
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
        _warn("roar ray driver entrypoint requires a command after --")
        return 2

    service: ProxyService | None = None
    handle: ProxyHandle | None = None
    child_env = os.environ.copy()
    started_at = time.time()

    try:
        try:
            service, handle = _start_driver_proxy()
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
                )
                if fragment is not None:
                    _emit_driver_proxy_fragment(fragment)
            except Exception as exc:
                _warn(f"[roar-driver] failed to collect local S3 proxy lineage: {exc}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
