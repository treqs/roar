from __future__ import annotations

from roar.application.publish.job_preparation import (
    estimate_links as estimate_links_full,
)
from roar.application.publish.job_preparation import (
    normalize_jobs_for_registration as normalize_jobs_full,
)
from roar.application.publish.job_preparation import (
    order_jobs_for_registration as order_jobs_full,
)
from roar.application.publish.register_preview_jobs import (
    estimate_links as estimate_links_preview,
)
from roar.application.publish.register_preview_jobs import (
    normalize_jobs_for_registration as normalize_jobs_preview,
)
from roar.application.publish.register_preview_jobs import (
    order_jobs_for_registration as order_jobs_preview,
)


def test_register_preview_jobs_match_full_helpers_for_ray_submit_lineage() -> None:
    jobs = [
        {
            "id": 1,
            "job_uid": "local-submit",
            "step_number": 1,
            "timestamp": 10.0,
            "command": "ray job submit --address http://localhost:8265 -- python main.py",
            "job_type": None,
            "_outputs": [{"artifact_id": "driver-output"}],
        },
        {
            "id": 2,
            "job_uid": "noise-job",
            "parent_job_uid": "local-submit",
            "step_number": 2,
            "timestamp": 20.0,
            "command": "ray_task:unknown",
            "job_type": "ray_task",
            "_outputs": [{"artifact_id": "noise-output"}],
        },
        {
            "id": 3,
            "job_uid": "phase-job",
            "parent_job_uid": "noise-job",
            "step_number": 3,
            "timestamp": 30.0,
            "command": "ray_task:process_shard",
            "job_type": "ray_task",
            "_inputs": [{"artifact_id": "driver-output"}],
            "_outputs": [{"artifact_id": "task-output"}],
        },
    ]

    normalized_full = normalize_jobs_full(jobs)
    normalized_preview = normalize_jobs_preview(jobs)

    assert normalized_preview == normalized_full
    assert order_jobs_preview(normalized_preview) == order_jobs_full(normalized_full)
    assert estimate_links_preview(normalized_preview) == estimate_links_full(normalized_full)


def test_register_preview_jobs_match_full_helpers_for_legacy_driver_lineage() -> None:
    jobs = [
        {
            "id": 1,
            "job_uid": "driver",
            "step_number": 1,
            "timestamp": 10.0,
            "command": "python train.py",
            "job_type": None,
        },
        {
            "id": 2,
            "job_uid": "worker",
            "parent_job_uid": "missing-driver",
            "step_number": 2,
            "timestamp": 20.0,
            "command": "ray_task:process_batch",
            "job_type": "ray_task",
        },
    ]

    assert normalize_jobs_preview(jobs) == normalize_jobs_full(jobs)
