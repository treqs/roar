from __future__ import annotations

from unittest.mock import MagicMock, patch

from roar.application.publish.registration import (
    CompositeRegistrationCandidate,
    normalize_registration_source_type,
    preregister_lineage_composites,
    sync_publish_labels,
)


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
