"""
Dataset identifier inference from observed file paths.

This module derives stable dataset collection identifiers from run/put path sets.
It is intentionally conservative to keep false positives low.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from ...core.models.dataset_identifier import SPLIT_CANONICAL_MAP, SPLIT_SEGMENTS, DatasetIdentifier

_blake3: Any | None
try:
    import blake3 as _blake3
except Exception:  # pragma: no cover - fallback path is tested instead
    _blake3 = None

_MANIFEST_NAMES = {
    "manifest.json",
    "dataset_info.json",
    "data.yaml",
    "dataset-card.md",
    "_success",
    "extraction_manifest.json",
    "block_manifest.json",
    "ray_manifest.json",
}
_CONTAINER_INTERNAL_DIRS = {"_versions", "_transactions", "data"}

_PAYLOAD_EXTS = {
    ".mcap",
    ".lance",
    ".parquet",
    ".tfrecord",
    ".tar",
    ".csv",
    ".jsonl",
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".webp",
}
_METADATA_ONLY_EXTS = {".json", ".manifest", ".txn", ""}
_ANTI_DIR_NAMES = {
    "checkpoints",
    "wandb",
    "mlruns",
    ".git",
    ".venv",
    "__pycache__",
    "tmp",
    "temp",
}
_SESSION_SEGMENT_RE = re.compile(r"^session_\d+_[a-z0-9_-]+(?:\.lance)?$", re.IGNORECASE)
_VERSION_HINT_RE = re.compile(r"^(v\d+|\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
_PARTITION_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_-]*=[^/]+$", re.IGNORECASE)
_CLASS_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$", re.IGNORECASE)
_SHARD_FILENAME_RE = re.compile(
    r"^(?:part[-_]\d+(?:-of-\d+)?|shard[-_]\d+(?:-of-\d+)?|"
    r"[a-z0-9_-]+-\d{5,}-of-\d+|\d{5,})(?:\..+)?$",
    re.IGNORECASE,
)
_DATE_SEGMENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SUPPORTED_URL_SCHEMES = {"s3", "gs", "https", "http", "file"}

# Scoring weights
_MANIFEST_WEIGHT = 0.25
_HIGH_CARDINALITY_WEIGHT = 0.20
_SESSION_CLUSTER_WEIGHT = 0.20
_PAYLOAD_EXT_WEIGHT = 0.20
_CONTAINER_COLLAPSE_WEIGHT = 0.10
_PARTITION_COLLAPSE_WEIGHT = 0.15
_CLASS_FOLDER_WEIGHT = 0.15
_SHARD_CLUSTER_WEIGHT = 0.10
_EXPLICIT_ROOT_WEIGHT = 0.35
_ANTI_METADATA_ONLY_PENALTY = -0.35
_ANTI_VOLATILE_PATH_PENALTY = -0.25


@dataclass
class _ObservedPath:
    original: str
    scheme: str
    parts: tuple[str, ...]
    filename: str
    suffix: str


@dataclass
class _CandidateStats:
    path_hits: int = 0
    manifest_hits: int = 0
    payload_hits: int = 0
    container_collapses: int = 0
    partition_collapses: int = 0
    class_dir_collapses: int = 0
    shard_file_hits: int = 0
    explicit_root_hints: int = 0
    metadata_only: bool = True
    anti_dir_name_hit: bool = False
    session_segments: set[str] = field(default_factory=set)
    split_segments: set[str] = field(default_factory=set)
    version_hints: set[str] = field(default_factory=set)


class _PathNormalizer:
    """Normalize raw path strings into structured _ObservedPath objects."""

    def normalize(self, path: str, repo_root: str | None) -> _ObservedPath | None:
        if "://" in path:
            return self.normalize_url(path)
        return self.normalize_local(path, repo_root)

    def normalize_url(self, url: str) -> _ObservedPath | None:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in _SUPPORTED_URL_SCHEMES:
            return None

        if scheme == "file":
            parts = tuple(seg for seg in PurePosixPath(parsed.path).parts if seg not in {"", "/"})
            filename = parts[-1] if parts else ""
            suffix = PurePosixPath(filename).suffix.lower()
            return _ObservedPath(
                original=url, scheme="file", parts=parts, filename=filename, suffix=suffix
            )

        netloc = parsed.netloc.strip()
        path_parts = tuple(seg for seg in PurePosixPath(parsed.path).parts if seg not in {"", "/"})
        parts = (netloc, *path_parts) if netloc else path_parts
        filename = parts[-1] if parts else ""
        suffix = PurePosixPath(filename).suffix.lower()
        return _ObservedPath(
            original=url, scheme=scheme, parts=parts, filename=filename, suffix=suffix
        )

    def normalize_local(self, path: str, repo_root: str | None) -> _ObservedPath:
        path_obj = Path(path)
        if not path_obj.is_absolute() and repo_root:
            path_obj = Path(repo_root) / path_obj
        abs_path = Path(os.path.abspath(str(path_obj)))
        parts = tuple(seg for seg in abs_path.as_posix().split("/") if seg)
        filename = parts[-1] if parts else ""
        suffix = abs_path.suffix.lower()
        return _ObservedPath(
            original=str(abs_path),
            scheme="file",
            parts=parts,
            filename=filename,
            suffix=suffix,
        )


class _CandidateIdentifier:
    """Derive candidate dataset identifiers from observed paths."""

    def candidate_id(self, observed: _ObservedPath) -> tuple[str | None, bool, bool, bool, bool]:
        if len(observed.parts) < 2:
            return None, False, False, False, False

        parent_parts = list(observed.parts[:-1])
        used_container_collapse = False
        used_partition_collapse = False
        used_class_dir_collapse = False
        explicit_root_hint = False
        lower_name = observed.filename.lower()

        # Explicit directory/root hints from command args should map directly.
        if (
            observed.suffix == ""
            and lower_name not in _MANIFEST_NAMES
            and not lower_name.endswith(".manifest")
            and lower_name not in _CONTAINER_INTERNAL_DIRS
            and lower_name not in SPLIT_SEGMENTS
            and not _SESSION_SEGMENT_RE.match(lower_name)
        ):
            explicit_root_hint = True
            return (
                self.format_candidate_id(observed.scheme, observed.parts),
                used_container_collapse,
                used_partition_collapse,
                used_class_dir_collapse,
                explicit_root_hint,
            )

        # Collapse internal container paths first.
        if parent_parts and parent_parts[-1].lower() in _CONTAINER_INTERNAL_DIRS:
            parent_parts = parent_parts[:-1]
            used_container_collapse = True

        # Collapse session-like container leaves (session_0001_42, session_0001_42.lance).
        if parent_parts and _SESSION_SEGMENT_RE.match(parent_parts[-1]):
            parent_parts = parent_parts[:-1]

        # Collapse class-folder leaves for image datasets (root/split/class/image.jpg).
        if (
            observed.suffix in _PAYLOAD_EXTS
            and observed.suffix in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
            and parent_parts
        ):
            class_segment = parent_parts[-1]
            lower_class_segment = class_segment.lower()
            if (
                _CLASS_SEGMENT_RE.match(class_segment)
                and lower_class_segment not in SPLIT_SEGMENTS
                and lower_class_segment not in _ANTI_DIR_NAMES
                and not _SESSION_SEGMENT_RE.match(lower_class_segment)
                and not self.is_partition_segment(class_segment)
            ):
                parent_parts = parent_parts[:-1]
                used_class_dir_collapse = True

        # Collapse explicit split leaves.
        if parent_parts and parent_parts[-1].lower() in SPLIT_SEGMENTS:
            parent_parts = parent_parts[:-1]

        # Collapse trailing partition/date segments.
        while parent_parts and self.is_partition_segment(parent_parts[-1]):
            parent_parts = parent_parts[:-1]
            used_partition_collapse = True

        # Collapse plain YYYY/MM/DD suffixes when present.
        if len(parent_parts) >= 3 and self.looks_like_calendar_triplet(parent_parts[-3:]):
            parent_parts = parent_parts[:-3]
            used_partition_collapse = True

        if not parent_parts:
            return (
                None,
                used_container_collapse,
                used_partition_collapse,
                used_class_dir_collapse,
                explicit_root_hint,
            )

        return (
            self.format_candidate_id(observed.scheme, tuple(parent_parts)),
            used_container_collapse,
            used_partition_collapse,
            used_class_dir_collapse,
            explicit_root_hint,
        )

    def format_candidate_id(self, scheme: str, parts: tuple[str, ...]) -> str:
        if scheme == "file":
            return f"file:///{'/'.join(parts)}"

        if not parts:
            return f"{scheme}://"
        if len(parts) == 1:
            return f"{scheme}://{parts[0]}"
        return f"{scheme}://{parts[0]}/{'/'.join(parts[1:])}"

    def is_partition_segment(self, segment: str) -> bool:
        return bool(_PARTITION_SEGMENT_RE.match(segment) or _DATE_SEGMENT_RE.match(segment))

    @staticmethod
    def canonicalize_split(split: str) -> str | None:
        if split not in SPLIT_SEGMENTS:
            return None
        return SPLIT_CANONICAL_MAP.get(split, split)

    def looks_like_calendar_triplet(self, segments: list[str]) -> bool:
        year, month, day = segments
        if not (year.isdigit() and len(year) == 4 and 1970 <= int(year) <= 2100):
            return False
        if not (month.isdigit() and 1 <= int(month) <= 12):
            return False
        return day.isdigit() and 1 <= int(day) <= 31

    def is_shard_like_filename(self, filename: str) -> bool:
        return bool(_SHARD_FILENAME_RE.match(filename))


class _StatsAccumulator:
    """Accumulate statistics for a candidate dataset from observed paths."""

    def __init__(self, candidate_identifier: _CandidateIdentifier) -> None:
        self._candidate_identifier = candidate_identifier

    def accumulate(
        self,
        stats: _CandidateStats,
        observed: _ObservedPath,
        used_container_collapse: bool,
        used_partition_collapse: bool,
        used_class_dir_collapse: bool,
        explicit_root_hint: bool,
    ) -> None:
        stats.path_hits += 1
        lower_name = observed.filename.lower()

        if lower_name in _MANIFEST_NAMES or lower_name.endswith(".manifest"):
            stats.manifest_hits += 1
        if observed.suffix in _PAYLOAD_EXTS:
            stats.payload_hits += 1
        if used_container_collapse:
            stats.container_collapses += 1
        if used_partition_collapse:
            stats.partition_collapses += 1
        if used_class_dir_collapse:
            stats.class_dir_collapses += 1
        if explicit_root_hint:
            stats.explicit_root_hints += 1
        if observed.suffix in _PAYLOAD_EXTS and self._candidate_identifier.is_shard_like_filename(
            lower_name
        ):
            stats.shard_file_hits += 1

        if observed.suffix not in _METADATA_ONLY_EXTS:
            stats.metadata_only = False

        start_index = max(0, len(observed.parts) - 6)
        for index, segment in enumerate(observed.parts[start_index:], start=start_index):
            lower_segment = segment.lower()
            if lower_segment in _ANTI_DIR_NAMES:
                # Avoid penalizing common absolute-path prefixes like /tmp/... in test/dev roots.
                if observed.scheme == "file" and lower_segment in {"tmp", "temp"} and index <= 1:
                    continue
                stats.anti_dir_name_hit = True

        # Use only trailing path context to avoid false positives from absolute prefixes
        # like /home/<user>/dev/... being interpreted as split="dev".
        for segment in observed.parts[-6:]:
            lower_segment = segment.lower()
            canonical_split = self._candidate_identifier.canonicalize_split(lower_segment)
            if canonical_split:
                stats.split_segments.add(canonical_split)
            if _SESSION_SEGMENT_RE.match(lower_segment):
                stats.session_segments.add(lower_segment)
            if _VERSION_HINT_RE.match(lower_segment):
                stats.version_hints.add(lower_segment)


class _ConfidenceScorer:
    """Score candidate datasets based on accumulated statistics."""

    def score(self, stats: _CandidateStats) -> tuple[float, list[str]]:
        score = 0.0
        evidence: list[str] = []

        if stats.manifest_hits > 0:
            score += _MANIFEST_WEIGHT
            evidence.append("manifest_anchor")
        if stats.path_hits >= 8:
            score += _HIGH_CARDINALITY_WEIGHT
            evidence.append("high_cardinality")
        if len(stats.session_segments) >= 3:
            score += _SESSION_CLUSTER_WEIGHT
            evidence.append("session_cluster")
        if stats.payload_hits > 0:
            score += _PAYLOAD_EXT_WEIGHT
            evidence.append("payload_ext")
        if stats.container_collapses > 0:
            score += _CONTAINER_COLLAPSE_WEIGHT
            evidence.append("container_internal_collapse")
        if stats.partition_collapses > 0:
            score += _PARTITION_COLLAPSE_WEIGHT
            evidence.append("partition_collapse")
        if stats.class_dir_collapses > 0:
            score += _CLASS_FOLDER_WEIGHT
            evidence.append("class_folder_collapse")
        if stats.shard_file_hits >= 3:
            score += _SHARD_CLUSTER_WEIGHT
            evidence.append("shard_cluster")
        if stats.explicit_root_hints > 0:
            score += _EXPLICIT_ROOT_WEIGHT
            evidence.append("explicit_root_hint")

        if stats.metadata_only and stats.path_hits <= 5 and stats.explicit_root_hints == 0:
            score += _ANTI_METADATA_ONLY_PENALTY
            evidence.append("anti_metadata_only")
        if stats.anti_dir_name_hit:
            score += _ANTI_VOLATILE_PATH_PENALTY
            evidence.append("anti_volatile_path")

        score = max(0.0, min(1.0, score))
        return round(score, 2), evidence


class _Fingerprinter:
    """Generate stable fingerprints for dataset identifiers."""

    def fingerprint(self, dataset_id: str) -> tuple[str, str]:
        payload = f"roar:dataset-id:v1\0{dataset_id}".encode()
        if _blake3 is not None:
            return _blake3.blake3(payload).hexdigest(), "blake3"
        return hashlib.sha256(payload).hexdigest(), "sha256"


class DatasetIdentifierInferer:
    """Infer dataset identifiers from observed path lists."""

    def __init__(self) -> None:
        self._normalizer = _PathNormalizer()
        self._candidate_identifier = _CandidateIdentifier()
        self._accumulator = _StatsAccumulator(self._candidate_identifier)
        self._scorer = _ConfidenceScorer()
        self._fingerprinter = _Fingerprinter()

    def infer(
        self,
        paths: list[str],
        repo_root: str | None = None,
        min_confidence: float = 0.55,
    ) -> list[dict[str, object]]:
        """
        Infer candidate dataset identifiers.

        Returns candidates sorted by confidence desc and filtered by min confidence.
        """
        unique_paths = [p for p in dict.fromkeys(paths) if isinstance(p, str) and p.strip()]
        if not unique_paths:
            return []

        candidates: dict[str, _CandidateStats] = {}
        for raw_path in unique_paths:
            observed = self._normalizer.normalize(raw_path, repo_root)
            if not observed or not observed.parts:
                continue

            (
                candidate_id,
                used_container_collapse,
                used_partition_collapse,
                used_class_dir_collapse,
                explicit_root_hint,
            ) = self._candidate_identifier.candidate_id(observed)
            if not candidate_id:
                continue

            stats = candidates.setdefault(candidate_id, _CandidateStats())
            self._accumulator.accumulate(
                stats,
                observed,
                used_container_collapse,
                used_partition_collapse,
                used_class_dir_collapse,
                explicit_root_hint,
            )

        results: list[dict[str, object]] = []
        for dataset_id, stats in candidates.items():
            confidence, evidence = self._scorer.score(stats)
            if confidence < min_confidence:
                continue

            fingerprint, algorithm = self._fingerprinter.fingerprint(dataset_id)
            split = sorted(stats.split_segments)[0] if len(stats.split_segments) == 1 else None
            version_hint = sorted(stats.version_hints)[0] if stats.version_hints else None

            result: dict[str, object] = {
                "dataset_id": dataset_id,
                "dataset_fingerprint": fingerprint,
                "dataset_fingerprint_algorithm": algorithm,
                "confidence": confidence,
                "evidence": evidence,
                "observed_paths": stats.path_hits,
            }
            if split:
                result["split"] = split
            if version_hint:
                result["version_hint"] = version_hint
            validated = DatasetIdentifier.model_validate(result)
            results.append(validated.model_dump(mode="python", exclude_none=True))

        results.sort(key=self._result_sort_key)
        return results

    @staticmethod
    def _result_sort_key(item: dict[str, object]) -> tuple[float, str]:
        confidence = item.get("confidence")
        dataset_id = item.get("dataset_id")
        normalized_confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        normalized_dataset_id = dataset_id if isinstance(dataset_id, str) else ""
        return (-normalized_confidence, normalized_dataset_id)
