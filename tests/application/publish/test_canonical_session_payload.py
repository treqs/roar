from __future__ import annotations

from roar.application.publish.session import build_canonical_session_payload
from roar.core.interfaces.lineage import LineageData
from roar.core.interfaces.registration import GitContext


def _lineage() -> LineageData:
    return LineageData(
        jobs=[
            {
                "id": 9,
                "job_uid": "job-b",
                "command": "python train.py",
                "job_type": "run",
                "step_number": 2,
                "parent_job_uid": "job-a",
                "_inputs": [
                    {"artifact_hash": "input-b", "path": "data/b.csv"},
                    {"artifact_hash": "input-a", "path": "data/a.csv"},
                ],
                "_outputs": [
                    {"artifact_hash": "output-b", "path": "models/b.bin"},
                    {"artifact_hash": "output-a", "path": "models/a.bin"},
                ],
                "metadata": {"runner": "python", "image": "python:3.12"},
            },
            {
                "id": 3,
                "job_uid": "job-a",
                "command": "python prepare.py",
                "job_type": "run",
                "step_number": 1,
                "parent_job_uid": None,
                "_inputs": [],
                "_outputs": [{"artifact_hash": "prepared", "path": "prepared.csv"}],
                "metadata": {"runner": "python"},
            },
        ]
    )


def test_build_canonical_session_payload_normalizes_job_and_artifact_order() -> None:
    payload = build_canonical_session_payload(
        lineage=_lineage(),
        git_context=GitContext(repo="repo", commit="deadbeef", branch="main"),
        creator_identity="treqs:user:user-123",
    )

    assert payload["creator_identity"] == "treqs:user:user-123"
    assert [job["step_number"] for job in payload["jobs"]] == [1, 2]
    assert [item["hash"] for item in payload["jobs"][1]["inputs"]] == ["input-a", "input-b"]
    assert [item["hash"] for item in payload["jobs"][1]["outputs"]] == ["output-a", "output-b"]


def test_build_canonical_session_payload_changes_only_creator_identity_when_lineage_matches() -> (
    None
):
    lineage = _lineage()

    first = build_canonical_session_payload(
        lineage=lineage,
        git_context=GitContext(repo="repo", commit="deadbeef", branch="main"),
        creator_identity="treqs:user:user-123",
    )
    second = build_canonical_session_payload(
        lineage=lineage,
        git_context=GitContext(repo="repo", commit="deadbeef", branch="main"),
        creator_identity="glaas:user:user-456",
    )

    assert first["creator_identity"] == "treqs:user:user-123"
    assert second["creator_identity"] == "glaas:user:user-456"
    assert {k: v for k, v in first.items() if k != "creator_identity"} == {
        k: v for k, v in second.items() if k != "creator_identity"
    }
