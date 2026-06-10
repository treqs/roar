"""Unit tests for composite-aware digest handling in lineage service."""

from unittest.mock import Mock

from roar.db.services.lineage import DefaultLineageService


def test_resolve_artifact_by_hash_falls_back_to_composite_algorithm():
    artifact_repo = Mock()
    job_repo = Mock()

    artifact_repo.get_by_hash.side_effect = [
        None,  # blake3 lookup
        {"id": "artifact-composite"},  # composite-blake3 lookup
    ]

    service = DefaultLineageService(artifact_repo=artifact_repo, job_repo=job_repo)
    artifact = service._resolve_artifact_by_hash("a" * 64)

    assert artifact == {"id": "artifact-composite"}
    assert artifact_repo.get_by_hash.call_args_list[0].kwargs == {"algorithm": "blake3"}
    assert artifact_repo.get_by_hash.call_args_list[1].kwargs == {"algorithm": "composite-blake3"}


