"""Application tests for lineage-query DTO building."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from roar.application.query import LineageQueryRequest
from roar.application.query.lineage import build_lineage_summary


def test_build_lineage_summary_returns_typed_graph(tmp_path: Path) -> None:
    db_artifact = {"id": "art-1"}
    target_artifact = {
        "size": 12,
        "first_seen_path": str(tmp_path / "model.pkl"),
        "hashes": [{"algorithm": "blake3", "digest": "c" * 64}],
    }
    jobs = [
        {
            "job_uid": "job-1",
            "step_number": 2,
            "command": "python train.py",
            "timestamp": 10.0,
            "duration_seconds": 1.5,
            "exit_code": 0,
            "_inputs": [{"hash": "a" * 64, "path": "processed.csv", "size": 4}],
            "_outputs": [{"hash": "b" * 64, "path": "model.pkl", "size": 12}],
        }
    ]

    request = LineageQueryRequest(
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        artifact="cccccccc",
        output="json",
        depth=5,
    )
    request.roar_dir.mkdir()

    with patch("roar.application.query.lineage.create_database_context") as mock_db:
        mock_db.return_value.__enter__.return_value = SimpleNamespace(
            artifacts=SimpleNamespace(
                get_by_hash=lambda artifact_hash, algorithm=None: db_artifact
                if algorithm == "blake3"
                else None
            ),
            lineage=SimpleNamespace(
                get_filtered_lineage=lambda artifact_id, max_depth: (
                    target_artifact,
                    jobs,
                    set(),
                )
            ),
        )
        summary = build_lineage_summary(request)

    assert summary.artifact.hash == "c" * 64
    assert summary.artifact.path.endswith("model.pkl")
    assert len(summary.jobs) == 1
    assert summary.jobs[0].command == "python train.py"
    assert [artifact.hash for artifact in summary.artifacts] == ["a" * 64, "b" * 64, "c" * 64]
