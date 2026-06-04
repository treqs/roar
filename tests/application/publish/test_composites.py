from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from roar.application.publish.composite_builder import CompositeBuildResult
from roar.application.publish.composites import build_publish_composite_results
from roar.application.publish.metadata import (
    build_local_publish_composite_metadata_json,
    build_publish_composite_dataset_metadata_json,
    build_put_operation_metadata_json,
)
from roar.application.publish.put_composites import persist_local_put_composite_registration
from roar.application.publish.source_resolution import ResolvedSource


def _persist_and_capture_algorithm(payload_algorithm: str | None) -> str:
    """Run the local put-composite persist and return the algorithm it registered."""
    hashes = [{"algorithm": payload_algorithm}] if payload_algorithm is not None else []
    composite = CompositeBuildResult(
        root_path="/data/ds",
        digest="a" * 64,
        payload={"size": 10, "hashes": hashes, "components": [], "source_type": None},
        component_count_total=2,
        component_count_stored=2,
    )
    artifacts_repo = MagicMock()
    artifacts_repo.register.return_value = ("artifact-id", True)
    db_ctx = SimpleNamespace(artifacts=artifacts_repo, composites=MagicMock())
    persist_local_put_composite_registration(
        db_ctx=db_ctx,
        composite=composite,
        composite_registration={},
        dataset_identifiers=None,
        logger=MagicMock(),
    )
    _, kwargs = artifacts_repo.register.call_args
    return next(iter(kwargs["hashes"]))


def test_put_persist_uses_payload_algorithm_sha256() -> None:
    # A sha256-leaf composite (sha256-native source/destination) registers locally as
    # composite-sha256, not a hardcoded composite-blake3.
    assert _persist_and_capture_algorithm("composite-sha256") == "composite-sha256"


def test_put_persist_defaults_to_blake3() -> None:
    # Local bytes (S3/GCS puts) keep composite-blake3 — the destination imposes no
    # content hash. Also covers a payload missing the algorithm label.
    assert _persist_and_capture_algorithm("composite-blake3") == "composite-blake3"
    assert _persist_and_capture_algorithm(None) == "composite-blake3"


def test_build_publish_composite_results_groups_roots_in_sorted_order(tmp_path: Path) -> None:
    first_root = tmp_path / "b-dataset"
    second_root = tmp_path / "a-dataset"
    first_root.mkdir()
    second_root.mkdir()
    first_file = first_root / "one.txt"
    second_file = second_root / "two.txt"
    first_file.write_text("one", encoding="utf-8")
    second_file.write_text("two", encoding="utf-8")

    builder = MagicMock()
    builder.build_all_for_root.side_effect = [
        [
            CompositeBuildResult(
                root_path=str(second_root),
                digest="a" * 64,
                payload={"components": []},
                component_count_total=1,
                component_count_stored=1,
            )
        ],
        [
            CompositeBuildResult(
                root_path=str(first_root),
                digest="b" * 64,
                payload={"components": []},
                component_count_total=1,
                component_count_stored=1,
            )
        ],
    ]

    results = build_publish_composite_results(
        resolved_sources=[
            ResolvedSource(
                path=first_file.resolve(),
                exists=True,
                relative_key="one.txt",
                source_root=first_root,
            )
        ],
        hashes_by_path={str(first_file.resolve()): "1" * 64, str(second_file.resolve()): "2" * 64},
        session_hash="session-hash",
        source_type="s3",
        additional_composite_roots={
            second_root: [
                ResolvedSource(
                    path=second_file.resolve(),
                    exists=True,
                    relative_key="two.txt",
                    source_root=second_root,
                )
            ]
        },
        composite_builder=builder,
    )

    assert [item.root_path for item in results] == [str(second_root), str(first_root)]
    assert [call.kwargs["root_path"] for call in builder.build_all_for_root.call_args_list] == [
        second_root,
        first_root,
    ]


def test_build_publish_composite_dataset_metadata_json_merges_identifier_and_profile() -> None:
    metadata_json = build_publish_composite_dataset_metadata_json(
        root_path="/data/train",
        dataset_identifiers=[
            {
                "dataset_id": "file:///data/train",
                "version_hint": "v1",
            }
        ],
        components=[
            {
                "component_size": 42,
                "component_type": "text/csv",
                "relative_path": "train.csv",
            }
        ],
        component_count_total=1,
    )

    assert metadata_json is not None
    payload = json.loads(metadata_json)
    assert payload["dataset"]["dataset_id"] == "file:///data/train"
    assert payload["dataset"]["version_hint"] == "v1"
    assert payload["dataset"]["profile"]["profiled_components"] == 1


def test_build_local_publish_composite_metadata_json_includes_composite_and_dataset() -> None:
    metadata_json = build_local_publish_composite_metadata_json(
        composite=CompositeBuildResult(
            root_path="/data/train",
            digest="a" * 64,
            payload={
                "components": [
                    {
                        "component_size": 42,
                        "component_type": "text/csv",
                        "relative_path": "train.csv",
                    }
                ]
            },
            component_count_total=1,
            component_count_stored=1,
        ),
        dataset_identifiers=[
            {
                "dataset_id": "file:///data/train",
            }
        ],
    )

    payload = json.loads(metadata_json)
    assert payload["composite"]["component_count_total"] == 1
    assert payload["dataset"]["dataset_id"] == "file:///data/train"
    assert payload["dataset"]["profile"]["profiled_components"] == 1


def test_build_put_operation_metadata_json_preserves_publish_payload() -> None:
    metadata_json = build_put_operation_metadata_json(
        message="publish dataset",
        destination="s3://bucket/demo",
        destination_type="s3",
        artifact_urls={"artifact-1": "s3://bucket/demo/file.csv"},
        composite_registrations=[{"hash": "a" * 64}],
        lineage_composite_registrations=[{"hash": "b" * 64}],
        dataset_identifiers=[{"name": "demo"}],
        git_commit="deadbeef",
        git_tag="put/deadbeef",
        timestamp=123.0,
    )

    payload = json.loads(metadata_json)
    assert payload["put"]["destination"] == "s3://bucket/demo"
    assert payload["put"]["composites"] == [{"hash": "a" * 64}]
    assert payload["put"]["lineage_composites"] == [{"hash": "b" * 64}]
