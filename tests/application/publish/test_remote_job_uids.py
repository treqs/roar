from __future__ import annotations

from roar.application.publish.remote_job_uids import (
    build_remote_publication_job_uid,
    prepare_jobs_for_remote_publication,
)


def test_build_remote_publication_job_uid_is_deterministic_and_scoped_by_session_hash() -> None:
    first = build_remote_publication_job_uid("a" * 64, "job-1")
    repeated = build_remote_publication_job_uid("a" * 64, "job-1")
    different_session = build_remote_publication_job_uid("b" * 64, "job-1")
    different_job = build_remote_publication_job_uid("a" * 64, "job-2")

    assert first == repeated
    assert first != different_session
    assert first != different_job
    assert len(first) == 32


def test_prepare_jobs_for_remote_publication_maps_parent_relationships_without_mutating_input() -> (
    None
):
    jobs = [
        {
            "id": 1,
            "job_uid": "parent-job",
            "parent_job_uid": None,
            "step_number": 1,
        },
        {
            "id": 2,
            "job_uid": "child-job",
            "parent_job_uid": "parent-job",
            "step_number": 2,
        },
    ]

    prepared = prepare_jobs_for_remote_publication(jobs, "f" * 64)

    assert jobs[0].get("remote_job_uid") is None
    assert jobs[1].get("remote_parent_job_uid") is None

    assert prepared[0]["job_uid"] == "parent-job"
    assert prepared[1]["job_uid"] == "child-job"
    assert prepared[0]["remote_job_uid"] == build_remote_publication_job_uid("f" * 64, "parent-job")
    assert prepared[1]["remote_job_uid"] == build_remote_publication_job_uid("f" * 64, "child-job")
    assert prepared[1]["remote_parent_job_uid"] == prepared[0]["remote_job_uid"]
