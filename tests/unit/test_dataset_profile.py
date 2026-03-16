"""Tests for dataset profile derivation from composite component rows."""

from __future__ import annotations

from roar.execution.recording.dataset_profile import build_dataset_profile


def test_build_dataset_profile_summarizes_formats_splits_partitions_and_dates():
    components = [
        {
            "relative_path": "train/date=2026-02-20/part-000.parquet",
            "component_size": 100,
            "component_type": "application/octet-stream",
        },
        {
            "relative_path": "train/date=2026-02-21/part-001.parquet",
            "component_size": 110,
            "component_type": "application/octet-stream",
        },
        {
            "relative_path": "val/date=2026-02-22/part-002.parquet",
            "component_size": 120,
            "component_type": "application/octet-stream",
        },
        {
            "relative_path": "images/train/class_a/img-0001.jpg",
            "component_size": 50,
            "component_type": "image/jpeg",
        },
    ]

    profile = build_dataset_profile(components, total_components=8)

    assert profile is not None
    assert profile["profile_version"] == 1
    assert profile["is_partial"] is True
    assert profile["profiled_components"] == 4
    assert profile["total_components"] == 8
    assert profile["modality_hint"] == "mixed"

    size_summary = profile["size_summary"]
    assert size_summary["total_component_bytes"] == 380
    assert size_summary["min_component_bytes"] == 50
    assert size_summary["max_component_bytes"] == 120

    format_summary = profile["format_summary"]
    assert format_summary[0]["format"] == "parquet"
    assert format_summary[0]["component_count"] == 3
    assert format_summary[0]["total_bytes"] == 330

    split_summary = {item["split"]: item for item in profile["split_summary"]}
    assert split_summary["train"]["component_count"] == 3
    assert split_summary["val"]["component_count"] == 1

    partition_summary = {item["key"]: item for item in profile["partition_summary"]}
    assert partition_summary["date"]["distinct_values"] == 3
    assert "2026-02-20" in partition_summary["date"]["sample_values"]

    coverage = profile["time_coverage_hint"]
    assert coverage["min_date"] == "2026-02-20"
    assert coverage["max_date"] == "2026-02-22"


def test_build_dataset_profile_returns_none_when_no_valid_components():
    assert build_dataset_profile([]) is None
    assert build_dataset_profile([{"component_size": 10}]) is None


def test_build_dataset_profile_handles_invalid_sizes_and_detects_tabular_modality():
    profile = build_dataset_profile(
        [
            {
                "relative_path": "dataset/train/part-0.csv",
                "component_size": "42",
                "component_type": "text/csv",
            },
            {
                "relative_path": "dataset/train/part-1.csv",
                "component_size": "not-a-number",
                "component_type": "text/csv",
            },
        ],
        total_components=2,
    )

    assert profile is not None
    assert profile["is_partial"] is False
    assert profile["modality_hint"] == "tabular"
    assert profile["size_summary"]["total_component_bytes"] == 42
