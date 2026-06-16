"""Tests for dataset label helper functions."""

from __future__ import annotations

from roar.execution.recording.dataset_metadata import (
    build_dataset_label_metadata,
    build_dataset_metadata,
    find_matching_identifier,
)

# ---------------------------------------------------------------------------
# find_matching_identifier
# ---------------------------------------------------------------------------


class TestFindMatchingIdentifier:
    def test_exact_match(self):
        identifiers = [
            {"dataset_id": "file:///data/raw", "confidence": 0.9},
        ]
        result = find_matching_identifier("/data/raw", identifiers)
        assert result is not None
        assert result["dataset_id"] == "file:///data/raw"

    def test_trailing_slash_normalization(self):
        identifiers = [
            {"dataset_id": "file:///data/raw/", "confidence": 0.8},
        ]
        result = find_matching_identifier("/data/raw", identifiers)
        assert result is not None
        assert result["confidence"] == 0.8

        # Root path has trailing slash, identifier doesn't
        identifiers2 = [
            {"dataset_id": "file:///data/raw", "confidence": 0.8},
        ]
        result2 = find_matching_identifier("/data/raw/", identifiers2)
        assert result2 is not None

    def test_no_match(self):
        identifiers = [
            {"dataset_id": "file:///data/raw", "confidence": 0.9},
        ]
        result = find_matching_identifier("/data/other", identifiers)
        assert result is None

    def test_skips_non_file_schemes(self):
        identifiers = [
            {"dataset_id": "s3://bucket/data/raw", "confidence": 0.9},
            {"dataset_id": "file:///data/raw", "confidence": 0.7},
        ]
        result = find_matching_identifier("/data/raw", identifiers)
        assert result is not None
        assert result["confidence"] == 0.7

    def test_empty_identifiers(self):
        result = find_matching_identifier("/data/raw", [])
        assert result is None

    def test_returns_first_match(self):
        identifiers = [
            {"dataset_id": "file:///data/raw", "confidence": 0.9},
            {"dataset_id": "file:///data/raw", "confidence": 0.5},
        ]
        result = find_matching_identifier("/data/raw", identifiers)
        assert result is not None
        assert result["confidence"] == 0.9


# ---------------------------------------------------------------------------
# build_dataset_metadata
# ---------------------------------------------------------------------------


class TestBuildDatasetMetadata:
    def test_minimal_fields(self):
        identifier = {
            "dataset_id": "file:///data/raw",
            "dataset_fingerprint": "abcdef1234567890",
            "dataset_fingerprint_algorithm": "blake3",
            "confidence": 0.5,
            "evidence": ["high_cardinality"],
            "observed_paths": 10,
        }
        result = build_dataset_metadata(identifier)
        assert result == {
            "dataset_id": "file:///data/raw",
            "dataset_fingerprint": "abcdef1234567890",
            "dataset_fingerprint_algorithm": "blake3",
            "confidence": 0.5,
            "evidence": ["high_cardinality"],
            "observed_paths": 10,
        }
        assert "split" not in result
        assert "version_hint" not in result

    def test_ignores_unknown_keys(self):
        identifier = {
            "dataset_id": "file:///data/raw",
            "extra_key": "should_not_appear",
        }
        result = build_dataset_metadata(identifier)
        assert "extra_key" not in result
        assert result["dataset_id"] == "file:///data/raw"


# ---------------------------------------------------------------------------
# build_dataset_label_metadata
# ---------------------------------------------------------------------------


class TestBuildDatasetLabelMetadata:
    def test_includes_dataset_identity_and_modality_labels(self):
        identifier = {
            "dataset_id": "file:///data/imagenet",
            "dataset_fingerprint": "a1b2c3d4e5f67890",
            "dataset_fingerprint_algorithm": "blake3",
            "split": "train",
            "version_hint": "v2",
        }

        result = build_dataset_label_metadata(
            identifier,
            components=[
                {
                    "relative_path": "train/class_a/image-0001.jpg",
                    "component_size": 42,
                    "component_type": "image/jpeg",
                }
            ],
            component_count_total=1,
        )

        assert result == {
            "dataset": {
                "type": "dataset",
                "id": "file:///data/imagenet",
                "fingerprint": "a1b2c3d4e5f67890",
                "fingerprint_algorithm": "blake3",
                "split": "train",
                "version_hint": "v2",
                "modality": "image",
            }
        }
