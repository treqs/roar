"""
Composite artifact payload builder for directory-backed put sources.

Builds canonical composite-blake3 digests and GLaaS composite registration
payloads from resolved file leaves.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .resolver import ResolvedSource

_blake3: Any | None
try:
    import blake3 as _blake3
except Exception:  # pragma: no cover - dependency is required in runtime/test env
    _blake3 = None

_CANONICAL_ALGORITHM = "composite-blake3"
_MAX_STORED_COMPONENTS = 1000
_BLOOM_PREFIX = b"roar:composite-membership:v1\0"
_BLOOM_TARGET_FALSE_POSITIVE_RATE = 0.001  # 0.1%
_BLOOM_MIN_BITS = 2048
_BLOOM_MAX_HASHES = 12
_BLOOM_VERSION = 1
_MAX_PAYLOAD_BYTES = 90 * 1024


@dataclass(frozen=True)
class CompositeLeaf:
    """Canonical leaf for composite digest and payload generation."""

    relative_path: str
    digest: str
    size: int
    component_type: str | None
    leaf_kind: str = "file"


@dataclass(frozen=True)
class CompositeBuildResult:
    """Composite registration payload and metadata summary."""

    root_path: str
    digest: str
    component_count_total: int
    component_count_stored: int
    payload: dict[str, Any]


class CompositeArtifactBuilder:
    """Construct composite artifact payloads from directory leaves."""

    def __init__(
        self,
        max_stored_components: int = _MAX_STORED_COMPONENTS,
        max_payload_bytes: int = _MAX_PAYLOAD_BYTES,
    ) -> None:
        if max_stored_components <= 0:
            raise ValueError("max_stored_components must be positive")
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        self._max_stored_components = max_stored_components
        self._max_payload_bytes = max_payload_bytes

    def build_for_root(
        self,
        root_path: Path,
        resolved_sources: list[ResolvedSource],
        hashes_by_path: dict[str, str],
        session_hash: str,
        source_type: str | None,
    ) -> CompositeBuildResult | None:
        """
        Build composite registration payload for a single directory root.

        Returns None when no hashable leaf components are available.
        """
        leaves = self._collect_leaves(root_path, resolved_sources, hashes_by_path)
        if not leaves:
            return None

        leaves.sort(key=lambda leaf: leaf.relative_path.encode("utf-8"))
        composite_digest = self._compute_composite_digest(leaves)

        payload = self._build_payload(
            leaves=leaves,
            composite_digest=composite_digest,
            source_type=source_type,
            session_hash=session_hash,
        )
        stored_leaves = payload["components"]

        return CompositeBuildResult(
            root_path=str(root_path),
            digest=composite_digest,
            component_count_total=len(leaves),
            component_count_stored=len(stored_leaves),
            payload=payload,
        )

    def _build_payload(
        self,
        leaves: list[CompositeLeaf],
        composite_digest: str,
        source_type: str | None,
        session_hash: str,
    ) -> dict[str, Any]:
        total_components = len(leaves)
        max_candidate_count = min(total_components, self._max_stored_components)
        total_size = sum(leaf.size for leaf in leaves)

        bloom_membership_base = self._build_membership_index_base(leaves)

        def build_with_count(stored_count: int) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "hash": composite_digest,
                "hashes": [{"algorithm": _CANONICAL_ALGORITHM, "digest": composite_digest}],
                "size": total_size,
                "source_type": source_type,
                "session_hash": session_hash,
                "component_count_total": total_components,
                "components": [self._component_payload(leaf) for leaf in leaves[:stored_count]],
            }
            membership = dict(bloom_membership_base)
            membership["stored_components"] = stored_count
            payload["membership_index"] = membership
            return payload

        best_payload = build_with_count(max_candidate_count)
        if self._payload_size(best_payload) <= self._max_payload_bytes:
            return best_payload

        low = 1
        high = max_candidate_count
        best_fit_payload = build_with_count(1)
        while low <= high:
            mid = (low + high) // 2
            payload = build_with_count(mid)
            if self._payload_size(payload) <= self._max_payload_bytes:
                best_fit_payload = payload
                low = mid + 1
            else:
                high = mid - 1

        return best_fit_payload

    def _collect_leaves(
        self,
        root_path: Path,
        resolved_sources: list[ResolvedSource],
        hashes_by_path: dict[str, str],
    ) -> list[CompositeLeaf]:
        leaves: list[CompositeLeaf] = []

        for source in resolved_sources:
            relative_path = self._relative_path(root_path, source)
            if not relative_path:
                continue
            leaf_kind = "symlink" if source.path.is_symlink() else "file"
            digest = self._resolve_component_digest(source.path, hashes_by_path)
            if not digest:
                continue
            component_type = (
                "inode/symlink"
                if leaf_kind == "symlink"
                else self._guess_component_type(relative_path)
            )
            component_size = self._resolve_component_size(source.path, leaf_kind)

            leaves.append(
                CompositeLeaf(
                    relative_path=relative_path,
                    digest=digest,
                    size=component_size,
                    component_type=component_type,
                    leaf_kind=leaf_kind,
                )
            )

        return leaves

    @staticmethod
    def _resolve_component_digest(path: Path, hashes_by_path: dict[str, str]) -> str | None:
        if path.is_symlink():
            return CompositeArtifactBuilder._symlink_target_digest(path)
        return hashes_by_path.get(str(path))

    @staticmethod
    def _resolve_component_size(path: Path, leaf_kind: str) -> int:
        if leaf_kind == "symlink":
            return len(CompositeArtifactBuilder._symlink_target_bytes(path))
        return path.stat().st_size

    @staticmethod
    def _symlink_target_digest(path: Path) -> str | None:
        if _blake3 is None:
            raise RuntimeError("blake3 package is required for composite digest computation")

        try:
            target_bytes = CompositeArtifactBuilder._symlink_target_bytes(path)
        except OSError:
            return None
        return _blake3.blake3(target_bytes).hexdigest()

    @staticmethod
    def _symlink_target_bytes(path: Path) -> bytes:
        return os.readlink(path).encode("utf-8")

    def _compute_composite_digest(self, leaves: list[CompositeLeaf]) -> str:
        if _blake3 is None:
            raise RuntimeError("blake3 package is required for composite digest computation")

        hasher = _blake3.blake3()
        for digest in sorted(leaf.digest for leaf in leaves):
            hasher.update(bytes.fromhex(digest))
        return hasher.hexdigest()

    @staticmethod
    def _component_payload(leaf: CompositeLeaf) -> dict[str, Any]:
        return {
            "relative_path": leaf.relative_path,
            "leaf_kind": leaf.leaf_kind,
            "component_algorithm": "blake3",
            "component_digest": leaf.digest,
            "component_size": leaf.size,
            "component_type": leaf.component_type,
        }

    @staticmethod
    def _guess_component_type(relative_path: str) -> str | None:
        guessed, _encoding = mimetypes.guess_type(relative_path)
        return guessed

    @staticmethod
    def _relative_path(root_path: Path, source: ResolvedSource) -> str:
        if source.relative_key:
            return CompositeArtifactBuilder._normalize_relative_key(source.relative_key)

        try:
            relative = source.path.relative_to(root_path)
            return CompositeArtifactBuilder._normalize_relative_key(relative.as_posix())
        except ValueError:
            return CompositeArtifactBuilder._normalize_relative_key(source.path.name)

    @staticmethod
    def _normalize_relative_key(relative_key: str) -> str:
        key = relative_key.replace("\\", "/").lstrip("/")
        if key.startswith("./"):
            key = key[2:]
        return key

    def _build_membership_index(
        self,
        leaves: list[CompositeLeaf],
        stored_components: int,
    ) -> dict[str, Any]:
        membership = self._build_membership_index_base(leaves)
        membership["stored_components"] = stored_components
        return membership

    def _build_membership_index_base(self, leaves: list[CompositeLeaf]) -> dict[str, Any]:
        total_components = len(leaves)
        bloom_bits = self._choose_bloom_bits(total_components)
        bloom_hashes = self._choose_bloom_hashes(total_components, bloom_bits)
        bloom_bytes = bytearray((bloom_bits + 7) // 8)

        for leaf in leaves:
            key = f"blake3:{leaf.digest}".encode()
            h1, h2 = self._bloom_hash_pair(key)
            if h2 == 0:
                h2 = 1
            for index in range(bloom_hashes):
                bit_pos = (h1 + index * h2) % bloom_bits
                bloom_bytes[bit_pos // 8] |= 1 << (bit_pos % 8)

        return {
            "total_components": total_components,
            "stored_components": total_components,
            "bloom_filter_base64": base64.b64encode(bytes(bloom_bytes)).decode("ascii"),
            "bloom_bits": bloom_bits,
            "bloom_hashes": bloom_hashes,
            "bloom_version": _BLOOM_VERSION,
        }

    @staticmethod
    def _payload_size(payload: dict[str, Any]) -> int:
        return len(json.dumps(payload, separators=(",", ":")))

    @staticmethod
    def _choose_bloom_bits(total_components: int) -> int:
        if total_components <= 0:
            return _BLOOM_MIN_BITS

        bits_per_component = -math.log(_BLOOM_TARGET_FALSE_POSITIVE_RATE) / (math.log(2) ** 2)
        target = max(_BLOOM_MIN_BITS, math.ceil(total_components * bits_per_component))
        return ((target + 7) // 8) * 8

    @staticmethod
    def _choose_bloom_hashes(total_components: int, bloom_bits: int) -> int:
        ratio = bloom_bits / max(total_components, 1)
        hashes = round(ratio * math.log(2))
        return min(_BLOOM_MAX_HASHES, max(1, hashes))

    @staticmethod
    def _bloom_hash_pair(key: bytes) -> tuple[int, int]:
        if _blake3 is None:
            raise RuntimeError("blake3 package is required for composite digest computation")

        seed = _blake3.blake3(_BLOOM_PREFIX + key).digest()
        h1 = int.from_bytes(seed[0:8], byteorder="little", signed=False)
        h2 = int.from_bytes(seed[8:16], byteorder="little", signed=False)
        return h1, h2
