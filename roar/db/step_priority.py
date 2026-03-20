"""Local step-priority helpers for session step selection.

These helpers intentionally avoid importing execution backend discovery.
The hot read/query path only needs stable ordering for known local records,
not dynamic backend registration.
"""

from __future__ import annotations

from typing import Any

_NOISE_COMMANDS = {
    "ray_task:unknown",
    "ray_task:__init__",
    "ray_task:shutdown",
    "ray_task:s3_proxy",
    "ray_task:s3_driver_proxy",
    "ray_task:RoarNodeAgent.__init__",
}


def _get_job_value(job: Any, key: str) -> Any:
    if job is None:
        return None
    if isinstance(job, dict):
        return job.get(key)
    return getattr(job, key, None)


def step_sort_key(job: Any) -> tuple[int, float, int]:
    """Return a stable ordering key for choosing the visible step record."""
    return (
        step_priority(job),
        float(_get_job_value(job, "timestamp") or 0.0),
        int(_get_job_value(job, "id") or 0),
    )


def is_noise_job(job: Any) -> bool:
    """Return True when a job is local execution noise."""
    execution_role = str(_get_job_value(job, "execution_role") or "").strip().lower()
    if execution_role == "noise":
        return True

    command = str(_get_job_value(job, "command") or "")
    return command in _NOISE_COMMANDS


def is_task_job(job: Any) -> bool:
    """Return True when a job is a worker/task record."""
    execution_role = str(_get_job_value(job, "execution_role") or "").strip().lower()
    if execution_role == "task":
        return True
    if is_noise_job(job):
        return False

    command = str(_get_job_value(job, "command") or "")
    if command.startswith("ray_task:"):
        return True

    job_type = str(_get_job_value(job, "job_type") or "").strip().lower()
    return job_type == "ray_task"


def is_host_or_submit_job(job: Any) -> bool:
    """Return True when a job should be treated as the local host/submit record."""
    execution_role = str(_get_job_value(job, "execution_role") or "").strip().lower()
    if execution_role in {"host", "submit"}:
        return True

    command = str(_get_job_value(job, "command") or "")
    if command.startswith("ray job submit"):
        return True

    job_type = str(_get_job_value(job, "job_type") or "").strip().lower()
    return job_type in {"", "run"}


def step_priority(job: Any) -> int:
    """Classify a job into the same priority bands used for step resolution."""
    execution_role = str(_get_job_value(job, "execution_role") or "").strip().lower()
    if is_noise_job(job):
        return 1
    if execution_role == "phase":
        return 5
    if is_task_job(job):
        return 4
    if is_host_or_submit_job(job):
        return 6

    return 2
