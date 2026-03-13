from __future__ import annotations

from unittest.mock import MagicMock, patch

from roar.application.publish.registration import (
    CompositeRegistrationCandidate,
    normalize_registration_hashes,
    normalize_registration_source_type,
    prepare_batch_registration_artifacts,
    preregister_lineage_composites,
    register_publish_lineage,
    sync_publish_labels,
)
from roar.core.interfaces.registration import BatchRegistrationResult, GitContext


def test_preregister_lineage_composites_records_successes_and_failures() -> None:
    client = MagicMock()
    client.register_composite_artifact.side_effect = [
        ({"artifact_id": "comp-1", "created": True}, None),
        (None, "duplicate hash"),
    ]
    logger = MagicMock()
    errors: list[str] = []

    registrations = preregister_lineage_composites(
        glaas_client=client,
        payloads=[
            CompositeRegistrationCandidate(
                hash="a" * 64,
                root_path="/tmp/a",
                component_count_total=2,
                component_count_stored=2,
                payload={"hash": "a" * 64},
            ),
            CompositeRegistrationCandidate(
                hash="b" * 64,
                root_path="/tmp/b",
                component_count_total=3,
                component_count_stored=1,
                payload={"hash": "b" * 64},
            ),
        ],
        registration_errors=errors,
        logger=logger,
    )

    assert registrations == [
        {
            "lineage": True,
            "hash": "a" * 64,
            "root_path": "/tmp/a",
            "component_count_total": 2,
            "component_count_stored": 2,
            "registered": True,
            "artifact_id": "comp-1",
            "created": True,
        },
        {
            "lineage": True,
            "hash": "b" * 64,
            "root_path": "/tmp/b",
            "component_count_total": 3,
            "component_count_stored": 1,
            "registered": False,
            "error": "duplicate hash",
        },
    ]
    assert errors == [f"Lineage composite {('b' * 64)[:12]}: duplicate hash"]
    logger.debug.assert_called_once()


def test_register_publish_lineage_prepends_errors_and_syncs_labels() -> None:
    coordinator = MagicMock()
    coordinator.register_lineage.return_value = BatchRegistrationResult(
        session_registered=True,
        jobs_created=2,
        jobs_failed=0,
        artifacts_registered=3,
        artifacts_failed=0,
        links_created=4,
        links_failed=0,
        errors=["batch-error"],
    )
    client = MagicMock()

    with patch("roar.application.publish.registration.sync_publish_labels") as sync_labels:
        result = register_publish_lineage(
            coordinator=coordinator,
            glaas_client=client,
            session_hash="session-hash",
            git_context=GitContext(repo="repo", branch="main", commit="deadbeef"),
            jobs=[{"job_uid": "job-1"}],
            artifacts=[{"hashes": [{"algorithm": "blake3", "digest": "a" * 64}]}],
            db_ctx=MagicMock(),
            session_id=7,
            label_artifacts=[{"hash": "a" * 64}],
            pre_registration_errors=["pre-error"],
        )

    assert result.errors == ["pre-error", "batch-error"]
    sync_labels.assert_called_once()


def test_register_publish_lineage_skips_label_sync_without_session_context() -> None:
    coordinator = MagicMock()
    coordinator.register_lineage.return_value = BatchRegistrationResult(
        session_registered=True,
        jobs_created=0,
        jobs_failed=0,
        artifacts_registered=0,
        artifacts_failed=0,
        links_created=0,
        links_failed=0,
        errors=[],
    )

    with patch("roar.application.publish.registration.sync_publish_labels") as sync_labels:
        register_publish_lineage(
            coordinator=coordinator,
            glaas_client=MagicMock(),
            session_hash="session-hash",
            git_context=GitContext(repo="repo", branch="main", commit="deadbeef"),
            jobs=[],
            artifacts=[],
            db_ctx=None,
            session_id=None,
            label_artifacts=[],
        )

    sync_labels.assert_not_called()


def test_sync_publish_labels_appends_error_when_sync_fails() -> None:
    client = MagicMock()
    client.sync_labels.return_value = (None, "permission denied")
    db_ctx = MagicMock()
    errors: list[str] = []

    with patch(
        "roar.application.publish.registration.collect_label_sync_payloads",
        return_value=[{"label": "demo"}],
    ):
        sync_publish_labels(
            glaas_client=client,
            db_ctx=db_ctx,
            session_id=7,
            session_hash="session-hash",
            jobs=[{"job_uid": "job-1"}],
            artifacts=[{"hash": "a" * 64}],
            errors=errors,
        )

    assert errors == ["Label sync failed: permission denied"]


def test_sync_publish_labels_skips_empty_payloads() -> None:
    client = MagicMock()

    with patch(
        "roar.application.publish.registration.collect_label_sync_payloads",
        return_value=[],
    ):
        sync_publish_labels(
            glaas_client=client,
            db_ctx=MagicMock(),
            session_id=1,
            session_hash="session-hash",
            jobs=[],
            artifacts=[],
            errors=[],
        )

    client.sync_labels.assert_not_called()


def test_normalize_registration_source_type_accepts_only_remote_sources() -> None:
    assert normalize_registration_source_type("S3") == "s3"
    assert normalize_registration_source_type(" https ") == "https"
    assert normalize_registration_source_type("local") is None
    assert normalize_registration_source_type(None) is None


def test_normalize_registration_hashes_deduplicates_and_optionally_falls_back() -> None:
    assert normalize_registration_hashes(
        {
            "hashes": [
                {"algorithm": "BLAKE3", "digest": "A" * 64},
                {"algorithm": "blake3", "digest": "a" * 64},
                {"algorithm": "", "digest": "ignored"},
            ]
        }
    ) == [{"algorithm": "blake3", "digest": "a" * 64}]
    assert normalize_registration_hashes({"hash": "B" * 64}) == []
    assert normalize_registration_hashes({"hash": "B" * 64}, fallback_to_hash=True) == [
        {"algorithm": "blake3", "digest": "b" * 64}
    ]


def test_prepare_batch_registration_artifacts_filters_composites_and_prefers_blake3() -> None:
    prepared = prepare_batch_registration_artifacts(
        [
            {
                "kind": "composite",
                "hashes": [{"algorithm": "composite-blake3", "digest": "c" * 64}],
                "size": 11,
                "source_type": "local",
            },
            {
                "hashes": [
                    {"algorithm": "etag", "digest": "etag-digest-1"},
                    {"algorithm": "blake3", "digest": "B" * 64},
                ],
                "size": "100",
                "source_type": "S3",
            },
        ],
        "session-1",
        prefer_blake3_first=True,
    )

    assert prepared == [
        {
            "hashes": [
                {"algorithm": "blake3", "digest": "b" * 64},
                {"algorithm": "etag", "digest": "etag-digest-1"},
            ],
            "size": 100,
            "source_type": "s3",
            "session_hash": "session-1",
        }
    ]


def test_prepare_batch_registration_artifacts_uses_hash_fallback_when_requested() -> None:
    prepared = prepare_batch_registration_artifacts(
        [
            {
                "hash": "d" * 64,
                "size": 9,
                "source_type": "local",
            }
        ],
        "session-1",
        fallback_to_hash=True,
    )

    assert prepared == [
        {
            "hashes": [{"algorithm": "blake3", "digest": "d" * 64}],
            "size": 9,
            "source_type": None,
            "session_hash": "session-1",
        }
    ]
