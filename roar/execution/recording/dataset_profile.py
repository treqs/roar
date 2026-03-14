"""Dataset profile derivation for composite artifact metadata."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from ...core.models.dataset_identifier import SPLIT_CANONICAL_MAP

_PARTITION_SEGMENT_RE = re.compile(r"^([a-z][a-z0-9_-]{0,63})=(.+)$", re.IGNORECASE)
_DATE_TOKEN_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SUFFIX_FORMAT_MAP: dict[str, str] = {
    ".parquet": "parquet",
    ".csv": "csv",
    ".tsv": "tsv",
    ".jsonl": "jsonl",
    ".json": "json",
    ".lance": "lance",
    ".tfrecord": "tfrecord",
    ".orc": "orc",
    ".avro": "avro",
    ".txt": "txt",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".bmp": "bmp",
    ".webp": "webp",
}

_TABULAR_FORMATS = {"parquet", "csv", "tsv", "jsonl", "lance", "tfrecord", "orc", "avro"}
_IMAGE_FORMATS = {"jpeg", "png", "bmp", "webp"}
_TEXT_FORMATS = {"txt", "json"}

_MAX_FORMATS = 6
_MAX_SPLITS = 6
_MAX_PARTITION_KEYS = 6
_MAX_PARTITION_VALUES = 4


def build_dataset_profile(
    components: list[dict[str, Any]],
    *,
    total_components: int | None = None,
) -> dict[str, Any] | None:
    """Build a compact dataset profile from composite component rows."""
    normalized_components = _normalize_components(components)
    if not normalized_components:
        return None

    profiled_components = len(normalized_components)
    declared_total_components = _normalize_component_count(total_components)
    total = max(profiled_components, declared_total_components)
    is_partial = total > profiled_components

    format_counts: Counter[str] = Counter()
    format_bytes: dict[str, int] = defaultdict(int)
    split_counts: Counter[str] = Counter()
    split_bytes: dict[str, int] = defaultdict(int)
    partition_values: dict[str, Counter[str]] = defaultdict(Counter)
    observed_dates: set[str] = set()
    component_sizes: list[int] = []

    for component in normalized_components:
        relative_path = component["relative_path"]
        component_size = component["component_size"]
        component_type = component.get("component_type")
        path_parts = PurePosixPath(relative_path).parts

        component_sizes.append(component_size)

        format_name = _detect_format(relative_path, component_type)
        format_counts[format_name] += 1
        format_bytes[format_name] += component_size

        split = _detect_split(path_parts)
        if split:
            split_counts[split] += 1
            split_bytes[split] += component_size

        for key, value in _extract_partitions(path_parts):
            partition_values[key][value] += 1
            maybe_date = _normalize_date_token(value)
            if maybe_date:
                observed_dates.add(maybe_date)

        for segment in path_parts:
            maybe_date = _normalize_date_token(segment)
            if maybe_date:
                observed_dates.add(maybe_date)

    profile: dict[str, Any] = {
        "profile_version": 1,
        "is_partial": is_partial,
        "profiled_components": profiled_components,
        "total_components": total,
        "modality_hint": _derive_modality_hint(format_counts),
        "size_summary": _build_size_summary(component_sizes),
        "format_summary": _build_frequency_summary(
            format_counts,
            format_bytes,
            key_name="format",
            max_items=_MAX_FORMATS,
        ),
    }

    split_summary = _build_frequency_summary(
        split_counts,
        split_bytes,
        key_name="split",
        max_items=_MAX_SPLITS,
    )
    if split_summary:
        profile["split_summary"] = split_summary

    partition_summary = _build_partition_summary(partition_values)
    if partition_summary:
        profile["partition_summary"] = partition_summary

    if observed_dates:
        ordered_dates = sorted(observed_dates)
        profile["time_coverage_hint"] = {
            "min_date": ordered_dates[0],
            "max_date": ordered_dates[-1],
        }

    return profile


def _normalize_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for component in components:
        relative_path = component.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path:
            continue

        component_size = _normalize_component_size(component.get("component_size"))
        component_type = component.get("component_type")
        if component_type is not None and not isinstance(component_type, str):
            component_type = None

        normalized.append(
            {
                "relative_path": relative_path,
                "component_size": component_size,
                "component_type": component_type,
            }
        )

    return normalized


def _normalize_component_count(value: int | None) -> int:
    try:
        if value is None:
            return 0
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _normalize_component_size(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float | str):
            return max(0, int(value))
    except (TypeError, ValueError):
        return 0
    return 0


def _detect_format(relative_path: str, component_type: str | None) -> str:
    lower_path = relative_path.lower()
    if lower_path.endswith(".tar.gz"):
        return "tar.gz"

    suffix = PurePosixPath(lower_path).suffix
    format_name = _SUFFIX_FORMAT_MAP.get(suffix)
    if format_name:
        return format_name

    if component_type:
        lower_type = component_type.lower()
        if lower_type.startswith("image/"):
            return "image"
        if lower_type.startswith("text/"):
            return "text"
        if "json" in lower_type:
            return "json"
        if "csv" in lower_type:
            return "csv"
        if "parquet" in lower_type:
            return "parquet"

    return "unknown"


def _detect_split(path_parts: tuple[str, ...]) -> str | None:
    for segment in path_parts:
        canonical = SPLIT_CANONICAL_MAP.get(segment.lower())
        if canonical:
            return canonical
    return None


def _extract_partitions(path_parts: tuple[str, ...]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for segment in path_parts:
        match = _PARTITION_SEGMENT_RE.match(segment)
        if not match:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip()
        if not value:
            continue
        pairs.append((key, value))
    return pairs


def _normalize_date_token(value: str) -> str | None:
    if not _DATE_TOKEN_RE.match(value):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _derive_modality_hint(format_counts: Counter[str]) -> str:
    modality_counts: Counter[str] = Counter()
    for format_name, count in format_counts.items():
        if format_name in _TABULAR_FORMATS:
            modality_counts["tabular"] += count
        elif format_name in _IMAGE_FORMATS or format_name == "image":
            modality_counts["image"] += count
        elif format_name in _TEXT_FORMATS or format_name == "text":
            modality_counts["text"] += count

    if not modality_counts:
        return "unknown"

    total = sum(modality_counts.values())
    ordered = modality_counts.most_common()
    if len(ordered) > 1 and ordered[0][1] / max(total, 1) < 0.80:
        return "mixed"
    return ordered[0][0]


def _build_size_summary(sizes: list[int]) -> dict[str, Any]:
    total_bytes = sum(sizes)
    avg_bytes = round(total_bytes / len(sizes), 2) if sizes else 0.0
    return {
        "total_component_bytes": total_bytes,
        "avg_component_bytes": avg_bytes,
        "min_component_bytes": min(sizes) if sizes else 0,
        "max_component_bytes": max(sizes) if sizes else 0,
    }


def _build_frequency_summary(
    counts: Counter[str],
    byte_totals: dict[str, int],
    *,
    key_name: str,
    max_items: int,
) -> list[dict[str, Any]]:
    if not counts:
        return []

    summary: list[dict[str, Any]] = []
    ordered_keys = sorted(counts, key=lambda item: (-counts[item], item))
    for key in ordered_keys[:max_items]:
        summary.append(
            {
                key_name: key,
                "component_count": int(counts[key]),
                "total_bytes": int(byte_totals.get(key, 0)),
            }
        )
    return summary


def _build_partition_summary(partition_values: dict[str, Counter[str]]) -> list[dict[str, Any]]:
    if not partition_values:
        return []

    summary: list[dict[str, Any]] = []
    ordered_keys = sorted(
        partition_values,
        key=lambda key: (-len(partition_values[key]), key),
    )

    for key in ordered_keys[:_MAX_PARTITION_KEYS]:
        counter = partition_values[key]
        ordered_values = sorted(counter, key=lambda value: (-counter[value], value))
        summary.append(
            {
                "key": key,
                "distinct_values": len(counter),
                "sample_values": ordered_values[:_MAX_PARTITION_VALUES],
            }
        )

    return summary
