"""Application tests for status-query DTO building."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from roar.application.query import StatusQueryRequest
from roar.application.query.status import StatusQueryError, build_status_summary


def test_build_status_summary_groups_steps_and_artifacts(tmp_path: Path) -> None:
    artifact_path = tmp_path / "model.pkl"
    artifact_path.write_text("model")
    missing_path = tmp_path / "missing.pkl"

    db_ctx = SimpleNamespace(
        sessions=SimpleNamespace(get_active=lambda: {"id": 7}),
        jobs=SimpleNamespace(
            get_by_session=lambda _session_id, limit=10000: [
                {"id": 1, "step_number": 1, "job_type": None},
                {"id": 2, "step_number": 2, "job_type": "build"},
            ],
            get_outputs=lambda job_id: (
                [
                    {
                        "artifact_id": 11,
                        "artifact_hash": "a" * 64,
                        "size": 5,
                        "path": str(artifact_path),
                    }
                ]
                if job_id == 1
                else [
                    {
                        "artifact_id": 12,
                        "artifact_hash": "b" * 64,
                        "size": 8,
                        "path": str(missing_path),
                    }
                ]
            ),
        ),
    )

    request = StatusQueryRequest(roar_dir=tmp_path / ".roar")
    request.roar_dir.mkdir()

    from unittest.mock import patch

    with (
        patch("roar.application.query.status.create_query_database_context") as mock_db,
        patch(
            "roar.application.query.status.load_publish_auth_context",
            return_value=SimpleNamespace(),
        ),
        patch(
            "roar.application.query.status.resolve_publish_creator_identity",
            return_value="anonymous",
        ),
        patch(
            "roar.application.query.status.compute_canonical_lineage_session_hash",
            return_value="canonical-session-hash",
        ) as compute_hash,
    ):
        mock_db.return_value.__enter__.return_value = db_ctx
        summary = build_status_summary(request)

    assert summary is not None
    assert summary.dag_hash == "canonical-session-hash"
    compute_hash.assert_called_once()
    assert summary.build_steps == 1
    assert summary.run_steps == 1
    assert len(summary.artifacts) == 2
    assert [artifact.present for artifact in summary.artifacts] == [True, False]


def test_build_status_summary_without_active_session_raises_query_error(tmp_path: Path) -> None:
    request = StatusQueryRequest(roar_dir=tmp_path / ".roar")
    request.roar_dir.mkdir()

    from unittest.mock import patch

    with patch("roar.application.query.status.create_query_database_context") as mock_db:
        mock_db.return_value.__enter__.return_value = SimpleNamespace(
            sessions=SimpleNamespace(get_active=lambda: None)
        )
        with pytest.raises(
            StatusQueryError,
            match=r"No active session\. Run 'roar run' to create a session first\.",
        ):
            build_status_summary(request)
