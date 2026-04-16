from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.query.diff_graph import LineageGraph, build_diff_graph, glaas_payload_to_graph


class TestGlaasPayloadToGraph:
    def test_reads_camel_case_jobs_and_flattens_children(self):
        payload = {
            "artifact": {
                "hashes": [{"algorithm": "blake3", "digest": "target-hash"}],
                "first_seen_path": "/output/model.pt",
            },
            "jobs": [
                {
                    "id": "parent-id",
                    "jobUid": "parent-job",
                    "command": "python train.py --epochs 2",
                    "jobType": "run",
                    "stepNumber": 2,
                    "gitCommit": "a" * 40,
                    "gitBranch": "main",
                    "metadata": {"role": "driver"},
                    "inputs": [{"hash": "checkpoint-hash", "path": "/input/checkpoint.pt"}],
                    "outputs": [{"hash": "final-hash", "path": "/output/model.pt"}],
                    "children": [
                        {
                            "id": "child-id",
                            "jobUid": "child-job",
                            "command": "python -m pkg.worker --rank 0",
                            "jobType": "ray_task",
                            "stepNumber": 1,
                            "gitCommit": "b" * 40,
                            "gitBranch": "main",
                            "metadata": '{"worker": 1}',
                            "inputs": [{"hash": "raw-hash", "path": "/input/raw.csv"}],
                            "outputs": [
                                {"hash": "checkpoint-hash", "path": "/output/checkpoint.pt"}
                            ],
                            "children": [],
                        }
                    ],
                }
            ],
        }

        graph = glaas_payload_to_graph("abc123", payload)

        assert graph.target_hash == "target-hash"
        assert graph.target_path == "/output/model.pt"
        assert [job.job_uid for job in graph.jobs] == ["child-job", "parent-job"]

        child_job, parent_job = graph.jobs
        assert child_job.step_number == 1
        assert child_job.git_commit == "b" * 40
        assert child_job.git_branch == "main"
        assert child_job.metadata == {"worker": 1}
        assert child_job.input_hashes == {"raw-hash": "raw-hash"}
        assert child_job.output_hashes == {"checkpoint-hash": "checkpoint-hash"}

        assert parent_job.step_number == 2
        assert parent_job.git_commit == "a" * 40
        assert parent_job.git_branch == "main"
        assert parent_job.metadata == {"role": "driver"}
        assert parent_job.input_hashes == {"checkpoint-hash": "checkpoint-hash"}
        assert parent_job.output_hashes == {"final-hash": "final-hash"}


def test_build_diff_graph_falls_back_to_remote_artifact_hash_when_enabled(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.jobs.get_by_uid.return_value = None
    db_ctx.artifacts.get_by_hash.return_value = None

    remote_graph = LineageGraph(
        target_artifact_id="remote:abc",
        target_hash="a" * 64,
        target_path=None,
        jobs=[],
    )

    with (
        patch(
            "roar.application.query.diff_graph.remote_artifact_fallback_enabled", return_value=True
        ),
        patch(
            "roar.application.query.diff_graph.build_remote_artifact_graph",
            return_value=remote_graph,
        ) as build_remote,
    ):
        graph = build_diff_graph(db_ctx, "a" * 64, tmp_path, 10)

    assert graph == remote_graph
    build_remote.assert_called_once_with("a" * 64, 10)


def test_build_diff_graph_does_not_remote_fallback_when_disabled(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.jobs.get_by_uid.return_value = None
    db_ctx.artifacts.get_by_hash.return_value = None

    with (
        patch(
            "roar.application.query.diff_graph.remote_artifact_fallback_enabled", return_value=False
        ),
        patch("roar.application.query.diff_graph.build_remote_artifact_graph") as build_remote,
        pytest.raises(RuntimeError, match=r"Artifact not found"),
    ):
        build_diff_graph(db_ctx, "b" * 64, tmp_path, 10)

    build_remote.assert_not_called()
