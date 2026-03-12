"""Submit a Ray job that exercises the worker_process_setup_hook crash path."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import time
from typing import Any

import ray
from ray.job_submission import JobStatus, JobSubmissionClient

_DASHBOARD_URL = "http://127.0.0.1:8265"
_ENTRYPOINT = "python /app/tests/e2e/ray/jobs/setup_hook_probe.py --inner-probe"
_POLL_INTERVAL_SECONDS = 1.0
_TIMEOUT_SECONDS = 120.0
_PROBE_TASK_COUNT = 4
_TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.STOPPED,
}


@ray.remote(max_calls=1, max_retries=0)
def _worker_pid() -> int:
    return os.getpid()


def _run_inner_probe() -> None:
    ray.init(address="auto")
    try:
        refs = [_worker_pid.remote() for _ in range(_PROBE_TASK_COUNT)]
        pids = ray.get(refs, timeout=60)
        print(json.dumps({"phase": "inner_complete", "pids": pids}, sort_keys=True))
    finally:
        with contextlib.suppress(Exception):
            ray.shutdown()


def _wait_for_terminal_status(client: JobSubmissionClient, job_id: str) -> JobStatus:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    last_status: JobStatus | None = None

    while time.monotonic() < deadline:
        status = client.get_job_status(job_id)
        last_status = status
        if status in _TERMINAL_JOB_STATUSES:
            return status
        time.sleep(_POLL_INTERVAL_SECONDS)

    last_status_name = last_status.name if isinstance(last_status, JobStatus) else str(last_status)
    raise TimeoutError(f"Timed out waiting for Ray job {job_id}; last status={last_status_name}")


def _build_payload(client: JobSubmissionClient, job_id: str, status: JobStatus) -> dict[str, Any]:
    info = client.get_job_info(job_id)
    logs = client.get_job_logs(job_id)
    return {
        "driver_exit_code": getattr(info, "driver_exit_code", None),
        "entrypoint": getattr(info, "entrypoint", ""),
        "error_type": getattr(info, "error_type", ""),
        "job_id": job_id,
        "logs": logs,
        "message": getattr(info, "message", ""),
        "status": status.name,
    }


def _build_runtime_env(job_id: str) -> dict[str, Any]:
    return {
        "worker_process_setup_hook": "roar.services.execution.worker_bootstrap.startup",
        "env_vars": {
            "PYTHONPATH": "/app/roar/services/execution/inject",
            "ROAR_JOB_ID": job_id,
            "ROAR_JOB_INSTRUMENTED": "1",
            "ROAR_PROJECT_DIR": "/app",
            "ROAR_RAY_NODE_AGENTS": "1",
            "ROAR_WRAP": "1",
        },
    }


def _submit_probe_job() -> int:
    client = JobSubmissionClient(_DASHBOARD_URL)
    probe_job_id = f"setup-hook-probe-{int(time.time())}"
    job_id = client.submit_job(
        entrypoint=_ENTRYPOINT,
        runtime_env=_build_runtime_env(probe_job_id),
    )
    status = _wait_for_terminal_status(client, job_id)
    payload = _build_payload(client, job_id, status)

    logs = str(payload.get("logs") or "")
    if logs:
        print(logs.rstrip())
    print(json.dumps(payload, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inner-probe",
        action="store_true",
        help="Run as the submitted Ray job entrypoint instead of submitting the job.",
    )
    args = parser.parse_args(argv)

    if args.inner_probe:
        _run_inner_probe()
        return 0

    return _submit_probe_job()


if __name__ == "__main__":
    raise SystemExit(main())
