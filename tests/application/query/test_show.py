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


def test_render_show_job_prefers_name_label_and_hides_duplicate_name_label_line(
    tmp_path: Path,
) -> None:
    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = {
            "id": 7,
            "job_uid": "deadbeef",
            "step_number": 2,
            "job_type": None,
            "step_name": "legacy-name",
            "timestamp": 1700000000.0,
            "duration_seconds": 1.25,
            "exit_code": 0,
            "command": "python train.py",
            "metadata": None,
            "telemetry": None,
        }
        db_ctx.jobs.get_inputs.return_value = []
        db_ctx.jobs.get_outputs.return_value = []
        db_ctx.labels = MagicMock()
        db_ctx.labels.get_current.return_value = {
            "metadata": {"name": "label-name", "phase": "train"}
        }

        rendered = show_module.render_show(_request(tmp_path, "deadbeef"))

    assert "Name:" in rendered
    assert "label-name" in rendered
    assert "phase=train" in rendered
    assert "name=label-name" not in rendered


def test_build_show_job_summary_falls_back_to_legacy_step_name_when_name_label_missing(
    tmp_path: Path,
) -> None:
    with patch.object(show_module, "create_query_database_context") as mock_db:
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = {
            "id": 7,
            "job_uid": "deadbeef",
            "step_number": 2,
            "job_type": None,
            "step_name": "legacy-name",
            "timestamp": 1700000000.0,
            "duration_seconds": 1.25,
            "exit_code": 0,
            "command": "python train.py",
            "metadata": None,
            "telemetry": None,
        }
        db_ctx.jobs.get_inputs.return_value = []
        db_ctx.jobs.get_outputs.return_value = []
        db_ctx.labels = MagicMock()
        db_ctx.labels.get_current.return_value = {"metadata": {"phase": "train"}}

        summary = show_module.build_show_summary(_request(tmp_path, "deadbeef"))

    assert isinstance(summary, ShowJobSummary)
    assert summary.step_name == "legacy-name"
    assert summary.labels == {"phase": "train"}


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


def _mock_glaas_client(mock_db) -> MagicMock:
    """Patch `show_module.GlaasClient` and return the instance it produces.

    Enrichment calls (lineage, composite components) default to a clean
    "nothing to add" response so tests that don't care about them stay
    deterministic without depending on the environment's GLaaS config.
    """
    client_instance = MagicMock()
    client_instance.get_artifact_lineage.return_value = (None, None)
    client_instance.get_composite_components.return_value = (None, None)
    mock_db.return_value = client_instance
    return client_instance


def test_build_show_summary_falls_back_to_remote_artifact_when_enabled(tmp_path: Path) -> None:
    full_hash = "a1b2c3d4e5f67890" * 4

    with (
        patch.object(show_module, "create_query_database_context") as mock_db,
        patch.object(show_module, "remote_artifact_fallback_enabled", return_value=True),
        patch.object(show_module, "GlaasClient") as mock_glaas_client,
        patch.object(
            show_module,
            "lookup_remote_artifact",
            return_value=(
                {
                    "hash": full_hash,
                    "size": "2048",
                    "registeredAt": "2026-04-16T12:34:56+00:00",
                    "metadata": {"dataset": {"dataset_id": "remote-ds"}},
                    "isComposite": False,
                },
                None,
            ),
        ) as lookup_remote,
    ):
        client_instance = _mock_glaas_client(mock_glaas_client)
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = None
        db_ctx.artifacts.get_by_hash.return_value = None

        summary = show_module.build_show_summary(_request(tmp_path, full_hash))

    assert isinstance(summary, ShowArtifactSummary)
    assert summary.id == full_hash
    assert summary.source == "remote"
    assert summary.metadata == {"dataset": {"dataset_id": "remote-ds"}}
    assert [hash_summary.digest for hash_summary in summary.hashes] == [full_hash]
    lookup_remote.assert_called_once_with(hash_prefix=full_hash, artifact_reader=client_instance)


def test_build_show_summary_remote_artifact_merges_labels_owner_and_producer(
    tmp_path: Path,
) -> None:
    full_hash = "b" * 64

    with (
        patch.object(show_module, "create_query_database_context") as mock_db,
        patch.object(show_module, "remote_artifact_fallback_enabled", return_value=True),
        patch.object(show_module, "GlaasClient") as mock_glaas_client,
        patch.object(
            show_module,
            "lookup_remote_artifact",
            return_value=(
                {
                    "hash": full_hash,
                    "size": "512",
                    "registeredAt": "2026-04-16T12:34:56+00:00",
                    "metadata": None,
                    "isComposite": False,
                    "scope": {
                        "owner_id": "org-1",
                        "owner_name": "acme",
                        "owner_type": "organization",
                        "project_id": "proj-1",
                        "project_name": "ml-project",
                        "visibility": "public",
                    },
                    "labels": [
                        {
                            "id": "lbl-1",
                            "entityType": "artifact",
                            "version": 1,
                            "metadata": {"env": "prod", "eval_accuracy": "0.94"},
                            "createdAt": "2026-04-16T12:00:00Z",
                            "sessionHash": None,
                        }
                    ],
                },
                None,
            ),
        ),
    ):
        client_instance = _mock_glaas_client(mock_glaas_client)
        client_instance.get_artifact_lineage.return_value = (
            {
                "producedBy": {
                    "jobUid": "job12345",
                    "command": "python train.py",
                    "sessionHash": "sess1234",
                }
            },
            None,
        )
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = None
        db_ctx.artifacts.get_by_hash.return_value = None

        summary = show_module.build_show_summary(_request(tmp_path, full_hash))

    assert isinstance(summary, ShowArtifactSummary)
    assert summary.labels == {"env": "prod", "eval_accuracy": "0.94"}
    assert summary.remote_owner == "acme/ml-project"
    assert summary.remote_visibility == "public"
    assert len(summary.produced_by) == 1
    assert summary.produced_by[0].job_uid == "job12345"
    assert summary.produced_by[0].command == "python train.py"
    assert summary.produced_by[0].session_hash == "sess1234"
    client_instance.get_artifact_lineage.assert_called_once_with(full_hash, depth=1)
    client_instance.get_composite_components.assert_not_called()


def test_build_show_summary_remote_composite_fetches_components(tmp_path: Path) -> None:
    full_hash = "c" * 64

    with (
        patch.object(show_module, "create_query_database_context") as mock_db,
        patch.object(show_module, "remote_artifact_fallback_enabled", return_value=True),
        patch.object(show_module, "GlaasClient") as mock_glaas_client,
        patch.object(
            show_module,
            "lookup_remote_artifact",
            return_value=(
                {"hash": full_hash, "size": "4096", "isComposite": True},
                None,
            ),
        ),
    ):
        client_instance = _mock_glaas_client(mock_glaas_client)
        client_instance.get_composite_components.return_value = (
            {
                "components": [
                    {
                        "relativePath": "part-0.parquet",
                        "leafKind": "file",
                        "componentDigest": "d" * 16,
                    }
                ]
            },
            None,
        )
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = None
        db_ctx.artifacts.get_by_hash.return_value = None

        summary = show_module.build_show_summary(_request(tmp_path, full_hash))

    assert summary.kind == "composite"
    assert len(summary.components) == 1
    assert summary.components[0].relative_path == "part-0.parquet"
    assert summary.components[0].leaf_kind == "file"
    client_instance.get_composite_components.assert_called_once_with(full_hash)


def test_build_show_summary_remote_artifact_swallows_enrichment_errors(tmp_path: Path) -> None:
    """A caller without visibility into the producing job (401/403, or any
    other enrichment error) still gets the base artifact info — enrichment
    is best-effort, not required for the lookup to succeed."""
    full_hash = "e" * 64

    with (
        patch.object(show_module, "create_query_database_context") as mock_db,
        patch.object(show_module, "remote_artifact_fallback_enabled", return_value=True),
        patch.object(show_module, "GlaasClient") as mock_glaas_client,
        patch.object(
            show_module,
            "lookup_remote_artifact",
            return_value=({"hash": full_hash, "size": "1", "isComposite": True}, None),
        ),
    ):
        client_instance = _mock_glaas_client(mock_glaas_client)
        client_instance.get_artifact_lineage.return_value = (None, "HTTP 403: forbidden")
        client_instance.get_composite_components.return_value = (None, "HTTP 403: forbidden")
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = None
        db_ctx.artifacts.get_by_hash.return_value = None

        summary = show_module.build_show_summary(_request(tmp_path, full_hash))

    assert isinstance(summary, ShowArtifactSummary)
    assert summary.id == full_hash
    assert summary.produced_by == []
    assert summary.components == []


def test_render_show_remote_artifact_includes_source_marker(tmp_path: Path) -> None:
    full_hash = "f" * 64

    with (
        patch.object(show_module, "create_query_database_context") as mock_db,
        patch.object(show_module, "remote_artifact_fallback_enabled", return_value=True),
        patch.object(show_module, "GlaasClient") as mock_glaas_client,
        patch.object(
            show_module,
            "lookup_remote_artifact",
            return_value=({"hash": full_hash, "size": "128", "registeredAt": 1}, None),
        ),
    ):
        _mock_glaas_client(mock_glaas_client)
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = None
        db_ctx.artifacts.get_by_hash.return_value = None

        rendered = show_module.render_show(_request(tmp_path, full_hash))

    assert "Source:" in rendered
    assert "GLaaS" in rendered
    assert full_hash in rendered


def test_build_show_summary_does_not_remote_fallback_when_disabled(tmp_path: Path) -> None:
    full_hash = "0" * 64

    with (
        patch.object(show_module, "create_query_database_context") as mock_db,
        patch.object(show_module, "remote_artifact_fallback_enabled", return_value=False),
        patch.object(show_module, "lookup_remote_artifact") as lookup_remote,
    ):
        db_ctx = MagicMock()
        mock_db.return_value.__enter__.return_value = db_ctx
        db_ctx.jobs.get_by_uid.return_value = None
        db_ctx.artifacts.get_by_hash.return_value = None

        with pytest.raises(show_module.ShowQueryError, match=f"Not found: {full_hash}"):
            show_module.build_show_summary(_request(tmp_path, full_hash))

    lookup_remote.assert_not_called()


def test_job_artifact_summary_skips_presence_check_for_composites() -> None:
    # A composite's "path" is a logical hf://… URI and its components may be
    # only partially materialized, so presence is left unknown (no "(missing)").
    summary = show_module._build_job_artifact_summary(
        {
            "path": "hf://datasets/owner/name@abc",
            "artifact_id": "aid-composite",
            "kind": "composite",
            "component_count": 6543,
            "size": 600_000_000_000,
            "hashes": [{"algorithm": "composite-sha256", "digest": "92fe50"}],
        }
    )
    assert summary.present is None


def test_job_artifact_summary_marks_missing_local_primitive(tmp_path: Path) -> None:
    missing_path = str(tmp_path / "gone.parquet")
    summary = show_module._build_job_artifact_summary(
        {
            "path": missing_path,
            "artifact_id": "aid-primitive",
            "kind": "primitive",
            "size": 10,
            "hashes": [{"algorithm": "blake3", "digest": "abc"}],
        }
    )
    assert summary.present is False


def test_job_artifact_summary_present_for_existing_primitive(tmp_path: Path) -> None:
    f = tmp_path / "here.parquet"
    f.write_text("x")
    summary = show_module._build_job_artifact_summary(
        {
            "path": str(f),
            "artifact_id": "aid-primitive",
            "kind": "primitive",
            "size": 1,
            "hashes": [{"algorithm": "blake3", "digest": "abc"}],
        }
    )
    assert summary.present is True
