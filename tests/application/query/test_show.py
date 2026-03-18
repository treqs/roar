"""Application tests for show-query orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import roar.application.query.show as show_module
from roar.application.query import ShowQueryRequest
from roar.application.query.results import ShowArtifactSummary, ShowJobSummary, ShowSessionSummary


def _request(
    tmp_path: Path,
    ref: str | None,
    *,
    selector: str = "auto",
) -> ShowQueryRequest:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    return ShowQueryRequest(roar_dir=roar_dir, cwd=tmp_path, ref=ref, selector=selector)


def test_render_show_artifact_by_full_hash(tmp_path: Path) -> None:
    full_hash = "a1b2c3d4e5f67890" * 4

    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = None
        db_ctx.artifacts.get_by_hash.return_value = {
            "id": "artifact-123",
            "size": 1024,
            "first_seen_at": 1700000000.0,
            "first_seen_path": "/data/model.pkl",
            "metadata": '{"dataset":{"dataset_id":"mnist"}}',
            "hashes": [
                {"algorithm": "blake3", "digest": full_hash},
                {"algorithm": "sha256", "digest": "sha256hash" * 6},
            ],
        }
        db_ctx.artifacts.get_locations.return_value = [{"path": "/data/model.pkl"}]
        db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

        summary = show_module.build_show_summary(_request(tmp_path, full_hash))

    assert isinstance(summary, ShowArtifactSummary)
    assert summary.id == "artifact-123"
    assert summary.metadata == {"dataset": {"dataset_id": "mnist"}}
    assert {hash_summary.algorithm for hash_summary in summary.hashes} == {"blake3", "sha256"}


def test_render_show_relative_path_resolves_to_absolute_lookup(tmp_path: Path) -> None:
    rel_path = "./data/model.pkl"
    expected_abs_path = str(tmp_path / "data" / "model.pkl")

    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = None

        with pytest.raises(
            show_module.ShowQueryError, match=f"No artifact found for path: {rel_path}"
        ):
            show_module.render_show(_request(tmp_path, rel_path))

    db_ctx.artifacts.get_by_path.assert_called_once_with(expected_abs_path)


def test_render_show_bare_filename_uses_absolute_path_lookup(tmp_path: Path) -> None:
    ref = "data.bin"
    expected_abs_path = str(tmp_path / ref)

    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = {
            "id": "artifact-456",
            "size": 12,
            "first_seen_at": 1700000000.0,
            "first_seen_path": expected_abs_path,
            "metadata": None,
            "hashes": [{"algorithm": "blake3", "digest": "a" * 64}],
        }
        db_ctx.artifacts.get_locations.return_value = [{"path": expected_abs_path}]
        db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

        summary = show_module.build_show_summary(_request(tmp_path, ref))

    db_ctx.artifacts.get_by_path.assert_called_once_with(expected_abs_path)
    assert isinstance(summary, ShowArtifactSummary)
    assert summary.id == "artifact-456"


def test_render_show_explicit_path_selector_bypasses_hex_auto_detection(tmp_path: Path) -> None:
    ref = "deadbeef"
    expected_abs_path = str(tmp_path / ref)

    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_path.return_value = {
            "id": "artifact-path",
            "size": 7,
            "first_seen_at": 1700000000.0,
            "first_seen_path": expected_abs_path,
            "metadata": None,
            "hashes": [{"algorithm": "blake3", "digest": "b" * 64}],
        }
        db_ctx.artifacts.get_locations.return_value = [{"path": expected_abs_path}]
        db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

        summary = show_module.build_show_summary(_request(tmp_path, ref, selector="path"))

    db_ctx.artifacts.get_by_path.assert_called_once_with(expected_abs_path)
    db_ctx.jobs.get_by_uid.assert_not_called()
    db_ctx.artifacts.get_by_hash.assert_not_called()
    assert isinstance(summary, ShowArtifactSummary)
    assert summary.id == "artifact-path"


def test_render_show_job_uid_takes_precedence_for_short_hex_refs(tmp_path: Path) -> None:
    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = {
            "id": 7,
            "job_uid": "deadbeef",
            "step_number": 2,
            "job_type": None,
            "timestamp": 1700000000.0,
            "duration_seconds": 1.25,
            "exit_code": 0,
            "command": "python train.py",
            "metadata": None,
            "telemetry": None,
        }
        db_ctx.jobs.get_inputs.return_value = []
        db_ctx.jobs.get_outputs.return_value = []

        summary = show_module.build_show_summary(_request(tmp_path, "deadbeef"))

    assert isinstance(summary, ShowJobSummary)
    assert summary.job_uid == "deadbeef"
    assert summary.command == "python train.py"


def test_render_show_explicit_artifact_selector_bypasses_job_uid_precedence(
    tmp_path: Path,
) -> None:
    full_hash = "deadbeef"

    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.artifacts.get_by_hash.return_value = {
            "id": "artifact-hash",
            "size": 33,
            "first_seen_at": 1700000000.0,
            "first_seen_path": "/data/model.pkl",
            "metadata": None,
            "hashes": [{"algorithm": "blake3", "digest": "c" * 64}],
        }
        db_ctx.artifacts.get_locations.return_value = [{"path": "/data/model.pkl"}]
        db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

        summary = show_module.build_show_summary(_request(tmp_path, full_hash, selector="artifact"))

    db_ctx.artifacts.get_by_hash.assert_called_once_with(full_hash)
    db_ctx.jobs.get_by_uid.assert_not_called()
    assert isinstance(summary, ShowArtifactSummary)
    assert summary.id == "artifact-hash"


def test_render_show_without_ref_returns_active_session_summary(tmp_path: Path) -> None:
    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.sessions.get_active.return_value = {
            "id": 3,
            "hash": "sess1234",
            "created_at": 1700000000.0,
            "git_repo": "/repo",
            "git_commit_start": "abc123",
        }
        db_ctx.jobs.get_by_session.return_value = [
            {
                "step_number": 1,
                "job_uid": "job12345",
                "exit_code": 0,
                "command": "python preprocess.py",
                "job_type": None,
            }
        ]

        summary = show_module.build_show_summary(_request(tmp_path, None))

    assert isinstance(summary, ShowSessionSummary)
    assert summary.hash == "sess1234"
    assert len(summary.jobs) == 1
    assert summary.jobs[0].command == "python preprocess.py"


def test_render_show_job_step_without_active_session_raises_query_error(tmp_path: Path) -> None:
    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.sessions.get_active.return_value = None

        with pytest.raises(
            show_module.ShowQueryError,
            match=r"No active session\. Run 'roar run' to create a session first\.",
        ):
            show_module.render_show(_request(tmp_path, "@1"))
