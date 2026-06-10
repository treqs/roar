from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from roar.application.publish.collection import (
    CollectedRegisterLineage,
    collect_register_lineage,
    resolve_local_session_target,
)
from roar.application.publish.targets import ResolvedRegisterTarget
from roar.core.interfaces.lineage import LineageData
from roar.publish_auth import PublishAuthContext


def test_collect_register_lineage_returns_missing_file_error(tmp_path: Path) -> None:
    collected, error = collect_register_lineage(
        target=ResolvedRegisterTarget(kind="artifact_path", value="missing.csv"),
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        lineage_collector=MagicMock(),
        session_service=MagicMock(),
        logger=MagicMock(),
    )

    assert collected is None
    assert error == "File not found: missing.csv"


def test_collect_register_lineage_resolves_s3_artifact_by_tracked_path(tmp_path: Path) -> None:
    collector = MagicMock()
    collector.collect.return_value = LineageData(
        jobs=[],
        artifacts=[],
        artifact_hashes={"etag123"},
        pipeline={"id": 1},
    )

    with patch("roar.application.publish.collection.create_database_context") as mock_ctx:
        db_ctx = MagicMock()
        db_ctx.__enter__ = MagicMock(return_value=db_ctx)
        db_ctx.__exit__ = MagicMock(return_value=None)
        db_ctx.artifacts.get_by_path.return_value = {
            "id": "artifact-1",
            "hashes": [{"algorithm": "etag", "digest": "etag123"}],
        }
        db_ctx.sessions.get_active.return_value = {"id": 1}
        mock_ctx.return_value = db_ctx

        collected, error = collect_register_lineage(
            target=ResolvedRegisterTarget(
                kind="artifact_path",
                value="s3://output-bucket/results/final.json",
            ),
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            lineage_collector=collector,
            session_service=MagicMock(),
            logger=MagicMock(),
        )

    assert error is None
    assert collected == CollectedRegisterLineage(
        lineage=collector.collect.return_value,
        session_id=1,
        artifact_hash="etag123",
        session_hash_override=None,
    )
    db_ctx.artifacts.get_by_path.assert_called_once_with("s3://output-bucket/results/final.json")
    collector.collect.assert_called_once_with(["etag123"], tmp_path / ".roar")


def test_collect_register_lineage_returns_no_active_session_for_artifact(tmp_path: Path) -> None:
    artifact_file = tmp_path / "tracked.csv"
    artifact_file.write_text("data", encoding="utf-8")

    with (
        patch(
            "roar.application.publish.collection.compute_hashes_batch",
            return_value={str(artifact_file): {"blake3": "a" * 64}},
        ),
        patch("roar.application.publish.collection.create_database_context") as mock_ctx,
    ):
        db_ctx = MagicMock()
        db_ctx.__enter__ = MagicMock(return_value=db_ctx)
        db_ctx.__exit__ = MagicMock(return_value=None)
        db_ctx.artifacts.get_by_hash.return_value = {
            "id": "artifact-1",
            "hashes": [{"algorithm": "blake3", "digest": "a" * 64}],
        }
        db_ctx.sessions.get_active.return_value = None
        mock_ctx.return_value = db_ctx

        collected, error = collect_register_lineage(
            target=ResolvedRegisterTarget(kind="artifact_path", value="tracked.csv"),
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            lineage_collector=MagicMock(),
            session_service=MagicMock(),
            logger=MagicMock(),
        )

    assert collected is None
    assert error == "No active session. Run 'roar run' to create a session first."


def test_resolve_local_session_target_returns_unique_prefix(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_all.return_value = [{"id": 1}, {"id": 2}]
    session_service = MagicMock()
    collector = MagicMock()
    collector.collect_session.side_effect = [
        LineageData(jobs=[{"job_uid": "job-1"}], pipeline={"id": 1}),
        LineageData(jobs=[{"job_uid": "job-2"}], pipeline={"id": 2}),
    ]

    with (
        patch(
            "roar.application.publish.collection.load_publish_auth_context",
            return_value=PublishAuthContext(
                access_token=None,
                scope_request=None,
                auth_provider=None,
                user_sub=None,
                db_user_id=None,
            ),
        ),
        patch(
            "roar.application.publish.collection.compute_canonical_lineage_session_hash",
            side_effect=["abc123456789", "ffffeeee1111"],
        ) as compute_hash,
    ):
        session, resolved_hash, error = resolve_local_session_target(
            db_ctx=db_ctx,
            roar_dir=tmp_path / ".roar",
            session_hash="abc123",
            session_service=session_service,
            lineage_collector=collector,
        )

    assert session == {"id": 1}
    assert resolved_hash == "abc123456789"
    assert error is None
    assert compute_hash.call_count == 2


def test_resolve_local_session_target_rejects_ambiguous_prefix(tmp_path: Path) -> None:
    db_ctx = MagicMock()
    db_ctx.sessions.get_all.return_value = [{"id": 1}, {"id": 2}]
    session_service = MagicMock()
    collector = MagicMock()
    collector.collect_session.side_effect = [
        LineageData(jobs=[{"job_uid": "job-1"}], pipeline={"id": 1}),
        LineageData(jobs=[{"job_uid": "job-2"}], pipeline={"id": 2}),
    ]

    with (
        patch(
            "roar.application.publish.collection.load_publish_auth_context",
            return_value=PublishAuthContext(
                access_token=None,
                scope_request=None,
                auth_provider=None,
                user_sub=None,
                db_user_id=None,
            ),
        ),
        patch(
            "roar.application.publish.collection.compute_canonical_lineage_session_hash",
            side_effect=["abc123456789", "abc123ffff00"],
        ),
    ):
        session, resolved_hash, error = resolve_local_session_target(
            db_ctx=db_ctx,
            roar_dir=tmp_path / ".roar",
            session_hash="abc123",
            session_service=session_service,
            lineage_collector=collector,
        )

    assert session is None
    assert resolved_hash is None
    assert error == (
        "Ambiguous session hash prefix 'abc123'. "
        "Provide more characters to select a single local session."
    )


def test_collect_register_lineage_for_job_uses_representative_hash(tmp_path: Path) -> None:
    collector = MagicMock()
    collector.collect_job.return_value = LineageData(
        jobs=[{"id": 1, "job_uid": "job-1"}],
        artifacts=[],
        artifact_hashes={"b" * 64},
        pipeline={"id": 9},
    )

    collected, error = collect_register_lineage(
        target=ResolvedRegisterTarget(kind="job_uid", value="job-1"),
        roar_dir=tmp_path / ".roar",
        cwd=tmp_path,
        lineage_collector=collector,
        session_service=MagicMock(),
        logger=MagicMock(),
    )

    assert error is None
    assert collected == CollectedRegisterLineage(
        lineage=collector.collect_job.return_value,
        session_id=9,
        artifact_hash="b" * 64,
        session_hash_override=None,
    )


def test_collect_register_lineage_step_reference_dry_run_uses_read_only_collector(
    tmp_path: Path,
) -> None:
    collector = MagicMock()
    collector.collect_step_read_only.return_value = LineageData(
        jobs=[{"id": 1, "job_uid": "job-1"}],
        artifacts=[],
        artifact_hashes={"c" * 64},
        pipeline={"id": 7},
    )

    with (
        patch("roar.application.publish.collection.create_database_context") as create_db_ctx,
        patch(
            "roar.application.publish.collection.create_query_database_context"
        ) as create_query_db,
    ):
        db_ctx = MagicMock()
        db_ctx.__enter__ = MagicMock(return_value=db_ctx)
        db_ctx.__exit__ = MagicMock(return_value=None)
        db_ctx.sessions.get_active.return_value = {"id": 7}
        create_query_db.return_value = db_ctx

        collected, error = collect_register_lineage(
            target=ResolvedRegisterTarget(kind="step_reference", value="@1"),
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            lineage_collector=collector,
            session_service=MagicMock(),
            logger=MagicMock(),
            dry_run=True,
        )

    assert error is None
    assert collected == CollectedRegisterLineage(
        lineage=collector.collect_step_read_only.return_value,
        session_id=7,
        artifact_hash="c" * 64,
        session_hash_override=None,
    )
    create_db_ctx.assert_not_called()
    collector.collect_step_read_only.assert_called_once_with(
        session_id=7,
        step_number=1,
        roar_dir=tmp_path / ".roar",
        job_type=None,
    )
