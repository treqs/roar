from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.composite_builder import CompositeArtifactBuilder
from roar.application.publish.registration import (
    CompositeRegistrationCandidate,
    build_lineage_composite_candidate,
    build_lineage_membership_index_payload,
    normalize_lineage_component_rows,
    normalize_registration_hashes,
    normalize_registration_source_type,
    prepare_batch_registration_artifacts,
    preregister_lineage_composites,
    register_publish_lineage,
    resolve_lineage_component_count_total,
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


@pytest.mark.parametrize(
    ("jobs_failed", "artifacts_failed"),
    [
        (1, 0),
        (0, 1),
    ],
)
def test_register_publish_lineage_skips_label_sync_when_target_registration_is_incomplete(
    jobs_failed: int,
    artifacts_failed: int,
) -> None:
    coordinator = MagicMock()
    coordinator.register_lineage.return_value = BatchRegistrationResult(
        session_registered=True,
        jobs_created=1,
        jobs_failed=jobs_failed,
        artifacts_registered=1,
        artifacts_failed=artifacts_failed,
        links_created=0,
        links_failed=0,
        errors=["registration-error"],
    )

    with patch("roar.application.publish.registration.sync_publish_labels") as sync_labels:
        result = register_publish_lineage(
            coordinator=coordinator,
            glaas_client=MagicMock(),
            session_hash="session-hash",
            git_context=GitContext(repo="repo", branch="main", commit="deadbeef"),
            jobs=[{"job_uid": "job-1"}],
            artifacts=[{"hashes": [{"algorithm": "blake3", "digest": "a" * 64}]}],
            db_ctx=MagicMock(),
            session_id=7,
            label_artifacts=[{"hash": "a" * 64}],
        )

    assert result.errors == ["registration-error"]
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


def test_sync_publish_labels_uses_registration_session_scoped_route() -> None:
    client = MagicMock()
    client.sync_labels_under_registration_session.return_value = (
        {"processed": 1, "created": 1},
        None,
    )

    with patch(
        "roar.application.publish.registration.collect_label_sync_payloads",
        return_value=[{"entity_type": "dag", "session_hash": "session-hash"}],
    ):
        sync_publish_labels(
            glaas_client=client,
            db_ctx=MagicMock(),
            session_id=7,
            session_hash="session-hash",
            jobs=[{"job_uid": "job-1"}],
            artifacts=[],
            errors=[],
            registration_session_id="reg-session-123",
        )

    client.sync_labels_under_registration_session.assert_called_once_with(
        "reg-session-123",
        [{"entity_type": "dag", "session_hash": "session-hash"}],
    )
    client.sync_labels.assert_not_called()


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


def test_resolve_lineage_component_count_total_prefers_best_available_count() -> None:
    assert resolve_lineage_component_count_total("2", {"total_components": 4}, 3) == 4
    assert resolve_lineage_component_count_total(None, {"total_components": "5"}, 2) == 5
    assert resolve_lineage_component_count_total(None, None, 2) == 2


def test_build_lineage_membership_index_payload_rebuilds_full_component_bloom() -> None:
    payload = build_lineage_membership_index_payload(
        composite_builder=CompositeArtifactBuilder(),
        membership_index={
            "total_components": 2,
            "stored_components": 2,
            "bloom_filter_base64": "AQIDBA==",
            "bloom_bits": 2048,
            "bloom_hashes": 12,
            "bloom_version": 1,
        },
        component_count_total=2,
        components=[
            {
                "relative_path": "part-000.json",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": "d" * 64,
                "component_size": 5,
                "component_type": "application/json",
            },
            {
                "relative_path": "part-001.json",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": "e" * 64,
                "component_size": 8,
                "component_type": "application/json",
            },
        ],
    )

    assert payload["total_components"] == 2
    assert payload["stored_components"] == 2
    assert payload["bloom_filter_base64"] != "AQIDBA=="
    assert payload["bloom_bits"] > 0
    assert payload["bloom_hashes"] > 0
    assert payload["bloom_version"] == 1


def test_build_lineage_membership_index_payload_preserves_sha256_algorithm() -> None:
    # F2: a fully-stored HF anchor rebuilt from lineage must key the bloom by the
    # component algorithm (sha256), not the CompositeLeaf default (blake3).
    import base64

    builder = CompositeArtifactBuilder()
    digest = "0" * 64
    payload = build_lineage_membership_index_payload(
        composite_builder=builder,
        membership_index={
            "total_components": 2,
            "stored_components": 2,
            "bloom_filter_base64": "AQ==",
            "bloom_bits": 2048,
            "bloom_hashes": 12,
            "bloom_version": 1,
        },
        component_count_total=2,
        components=[
            {
                "relative_path": "shard_00000.parquet",
                "leaf_kind": "file",
                "component_algorithm": "sha256",
                "component_digest": digest,
                "component_size": 1,
            },
            {
                "relative_path": "shard_00001.parquet",
                "leaf_kind": "file",
                "component_algorithm": "sha256",
                "component_digest": "1" * 64,
                "component_size": 1,
            },
        ],
    )

    bloom = base64.b64decode(payload["bloom_filter_base64"])
    bits, nhash = payload["bloom_bits"], payload["bloom_hashes"]

    def member(key: str) -> bool:
        h1, h2 = builder._bloom_hash_pair(key.encode())
        if h2 == 0:
            h2 = 1
        return all(
            (bloom[((h1 + i * h2) % bits) // 8] >> (((h1 + i * h2) % bits) % 8)) & 1
            for i in range(nhash)
        )

    assert member(f"sha256:{digest}") is True  # keyed by the component algorithm
    assert member(f"blake3:{digest}") is False  # the pre-fix mis-keying


def test_build_lineage_membership_index_payload_rejects_partial_component_set() -> None:
    with pytest.raises(ValueError, match="missing required bloom fields"):
        build_lineage_membership_index_payload(
            composite_builder=CompositeArtifactBuilder(),
            membership_index=None,
            component_count_total=2,
            components=[
                {
                    "relative_path": "subset.parquet",
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": "a" * 64,
                    "component_size": 123,
                    "component_type": "application/octet-stream",
                }
            ],
        )


def test_normalize_lineage_component_rows_uses_resolver_and_normalizes_fields() -> None:
    logger = MagicMock()

    components = normalize_lineage_component_rows(
        [
            {
                "relative_path": "part-000.json",
                "leaf_kind": "file",
                "component_size": "5",
                "component_type": "application/json",
            },
            {
                "relative_path": "part-001.json",
                "component_size": None,
            },
        ],
        resolve_component=lambda row: (
            ("blake3", "d" * 64)
            if row["relative_path"] == "part-000.json"
            else ("blake3", "E" * 64)
        ),
        logger=logger,
    )

    assert components == [
        {
            "relative_path": "part-000.json",
            "leaf_kind": "file",
            "component_algorithm": "blake3",
            "component_digest": "d" * 64,
            "component_size": 5,
            "component_type": "application/json",
        },
        {
            "relative_path": "part-001.json",
            "leaf_kind": "file",
            "component_algorithm": "blake3",
            "component_digest": "e" * 64,
            "component_size": 0,
            "component_type": None,
        },
    ]
    logger.warning.assert_not_called()


def test_normalize_lineage_component_rows_logs_missing_digest() -> None:
    logger = MagicMock()

    components = normalize_lineage_component_rows(
        [{"relative_path": "missing.parquet"}],
        resolve_component=lambda _row: None,
        logger=logger,
    )

    assert components == []
    logger.warning.assert_called_once()


def test_build_lineage_composite_candidate_builds_payload_with_metadata() -> None:
    candidate = build_lineage_composite_candidate(
        artifact={
            "first_seen_path": "/tmp/dataset",
            "component_count": 2,
            "size": "13",
            "source_type": "S3",
        },
        composite_digest="c" * 64,
        hashes=[{"algorithm": "composite-blake3", "digest": "c" * 64}],
        components=[
            {
                "relative_path": "part-000.json",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": "d" * 64,
                "component_size": 5,
                "component_type": "application/json",
            },
            {
                "relative_path": "part-001.json",
                "leaf_kind": "file",
                "component_algorithm": "blake3",
                "component_digest": "e" * 64,
                "component_size": 8,
                "component_type": "application/json",
            },
        ],
        membership_index=None,
        session_hash="session-hash",
        composite_builder=CompositeArtifactBuilder(),
        metadata='{"dataset": true}',
        logger=MagicMock(),
    )

    assert candidate is not None
    assert candidate.hash == "c" * 64
    assert candidate.root_path == "/tmp/dataset"
    assert candidate.component_count_total == 2
    assert candidate.payload["source_type"] == "s3"
    assert candidate.payload["size"] == 13
    assert candidate.payload["metadata"] == '{"dataset": true}'


def test_build_lineage_composite_candidate_returns_none_without_component_count() -> None:
    logger = MagicMock()

    candidate = build_lineage_composite_candidate(
        artifact={
            "first_seen_path": "/tmp/dataset",
            "component_count": None,
            "size": 13,
            "source_type": "local",
        },
        composite_digest="c" * 64,
        hashes=[{"algorithm": "composite-blake3", "digest": "c" * 64}],
        components=[],
        membership_index=None,
        session_hash="session-hash",
        composite_builder=CompositeArtifactBuilder(),
        logger=logger,
    )

    assert candidate is None
    logger.warning.assert_called_once()
