"""
Composite artifact payload builder for directory-backed publish sources.

Builds canonical composite-blake3 digests and GLaaS composite registration
payloads from resolved file leaves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...application.publish.source_resolution import ResolvedSource
from ..composite import detect as _detect
from ..composite import detect_nested as _detect_nested
from ..composite import preimage as _preimage

_blake3: Any | None
try:
    import blake3 as _blake3
except Exception:  # pragma: no cover - dependency is required in runtime/test env
    _blake3 = None

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
    component_algorithm: str = "blake3"


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
        return self.build_for_leaves(
            root_path=str(root_path),
            leaves=leaves,
            session_hash=session_hash,
            source_type=source_type,
        )

    def build_all_for_root(
        self,
        root_path: Path,
        resolved_sources: list[ResolvedSource],
        hashes_by_path: dict[str, str],
        session_hash: str,
        source_type: str | None,
        declared: bool = False,
    ) -> list[CompositeBuildResult]:
        """Build composite(s) for a root, forming a composite-of-composites when the
        root is a container of >=2 independently-structured sub-datasets.

        Returns ``[]`` when the input is not a dataset (an unstructured pile, unless
        ``declared``, or fewer than two identity-bearing files); ``[flat]`` for an
        ordinary dataset; or ``[child_1, ..., child_n, parent]`` for a nested
        container (the parent's hash is Merkle over the child composite hashes; its
        membership bloom is flat over every leaf blob).
        """
        raw = self._collect_raw_leaves(root_path, resolved_sources, hashes_by_path)
        if not raw:
            return []

        detection = _detect_nested([leaf.relative_path for leaf in raw])
        if detection.kind != "nested":
            return self._flat_or_none(root_path, raw, session_hash, source_type, declared)

        results: list[CompositeBuildResult] = []
        children: list[tuple[str, CompositeBuildResult]] = []
        child_identity_leaves: list[CompositeLeaf] = []
        root_str = str(root_path).rstrip("/")
        for prefix in detection.nested:
            sub = self._drop_boilerplate(
                [
                    self._reroot_leaf(leaf, prefix)
                    for leaf in raw
                    if leaf.relative_path.startswith(prefix + "/")
                ]
            )
            child = self.build_for_leaves(
                root_path=f"{root_str}/{prefix}",
                leaves=sub,
                session_hash=session_hash,
                source_type=source_type,
            )
            if child is not None:
                results.append(child)
                children.append((prefix, child))
                child_identity_leaves.extend(self._reparent_leaf(leaf, prefix) for leaf in sub)

        if len(children) < 2:
            # not actually a multi-dataset container after boilerplate/empty drops
            return self._flat_or_none(root_path, raw, session_hash, source_type, declared)

        results.append(
            self._build_parent(
                root_path=root_str,
                children=children,
                leaf_blobs=child_identity_leaves,
                session_hash=session_hash,
                source_type=source_type,
            )
        )
        return results

    def _flat_or_none(
        self,
        root_path: Path,
        raw: list[CompositeLeaf],
        session_hash: str,
        source_type: str | None,
        declared: bool,
    ) -> list[CompositeBuildResult]:
        """Form a single flat composite, or none when the input isn't a dataset.

        A composite requires >=2 identity-bearing leaves (boilerplate excluded) and
        either a detected structure or an explicit ``declared`` (roar put --dataset).
        An unstructured pile or a lone file registers as plain artifacts instead.
        """
        identity = self._drop_boilerplate(raw)
        if len(identity) < 2:
            return []
        if (
            not declared
            and _detect([leaf.relative_path for leaf in identity]).kind == "unstructured"
        ):
            return []
        flat = self.build_for_leaves(
            root_path=str(root_path),
            leaves=identity,
            session_hash=session_hash,
            source_type=source_type,
        )
        return [flat] if flat is not None else []

    @staticmethod
    def _reroot_leaf(leaf: CompositeLeaf, prefix: str) -> CompositeLeaf:
        """Return ``leaf`` with ``prefix/`` stripped from its relative path."""
        rel = leaf.relative_path[len(prefix) + 1 :]
        return CompositeLeaf(
            relative_path=rel,
            digest=leaf.digest,
            size=leaf.size,
            component_type=leaf.component_type,
            leaf_kind=leaf.leaf_kind,
            component_algorithm=leaf.component_algorithm,
        )

    @staticmethod
    def _reparent_leaf(leaf: CompositeLeaf, prefix: str) -> CompositeLeaf:
        """Return a child leaf with its parent-relative path restored (``prefix/...``)."""
        return CompositeLeaf(
            relative_path=f"{prefix}/{leaf.relative_path}",
            digest=leaf.digest,
            size=leaf.size,
            component_type=leaf.component_type,
            leaf_kind=leaf.leaf_kind,
            component_algorithm=leaf.component_algorithm,
        )

    def _build_parent(
        self,
        *,
        root_path: str,
        children: list[tuple[str, CompositeBuildResult]],
        leaf_blobs: list[CompositeLeaf],
        session_hash: str,
        source_type: str | None,
    ) -> CompositeBuildResult:
        """Build a parent composite-of-composites: Merkle hash over child hashes,
        flat membership bloom over every leaf blob."""
        children = sorted(children, key=lambda item: item[0].encode("utf-8"))
        combiner = self._combiner_algorithm(leaf_blobs)
        # Merkle: hash over (child_path, child_algorithm, child_digest)
        triples = [
            (prefix, child.payload["hashes"][0]["algorithm"], child.digest)
            for prefix, child in children
        ]
        pre = _preimage(triples, domain=f"roar:{combiner}-tree:v1\n".encode())
        if combiner == "blake3":
            if _blake3 is None:
                raise RuntimeError("blake3 package is required for composite digest computation")
            parent_digest = _blake3.blake3(pre).hexdigest()
        else:
            parent_digest = hashlib.new(combiner, pre).hexdigest()
        parent_algorithm = f"composite-{combiner}"

        components = [
            {
                "relative_path": prefix,
                "leaf_kind": "composite",
                "component_algorithm": child.payload["hashes"][0]["algorithm"],
                "component_digest": child.digest,
                "component_size": int(child.payload.get("size") or 0),
                "component_type": "application/vnd.roar.composite",
            }
            for prefix, child in children
        ]
        # Membership is flat over every leaf blob (total = blob count); the stored
        # components are the child composites (a structural rollup). component_count_total
        # tracks the blob population so it equals membership.total_components, and
        # stored_components equals the number of child entries — satisfying GLaaS's
        # composite count invariants for a composite-of-composites.
        total_blobs = len(leaf_blobs)
        membership = self._build_membership_index_base(leaf_blobs)
        membership["stored_components"] = len(components)
        payload: dict[str, Any] = {
            "hash": parent_digest,
            "hashes": [{"algorithm": parent_algorithm, "digest": parent_digest}],
            "size": sum(int(child.payload.get("size") or 0) for _prefix, child in children),
            "source_type": source_type,
            "session_hash": session_hash,
            "component_count_total": total_blobs,
            "components": components,
            "membership_index": membership,
        }
        return CompositeBuildResult(
            root_path=root_path,
            digest=parent_digest,
            component_count_total=total_blobs,
            component_count_stored=len(components),
            payload=payload,
        )

    def build_for_leaves(
        self,
        *,
        root_path: str,
        leaves: list[CompositeLeaf],
        session_hash: str,
        source_type: str | None,
    ) -> CompositeBuildResult | None:
        """Build a composite payload from already-resolved leaf components."""
        if not leaves:
            return None

        leaves.sort(key=lambda leaf: leaf.relative_path.encode("utf-8"))
        composite_digest, composite_algorithm = self._compute_composite_digest(leaves)

        payload = self._build_payload(
            leaves=leaves,
            composite_digest=composite_digest,
            composite_algorithm=composite_algorithm,
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
        composite_algorithm: str,
        source_type: str | None,
        session_hash: str,
    ) -> dict[str, Any]:
        """The FULL composite payload — every leaf is a stored component.

        The GLaaS upload is bounded separately by :meth:`cap_payload_for_upload` (payload
        size + paid GLaaS storage). Locally we keep the complete component set so
        membership/prune resolution (``find_by_component_digest``) is exact for any
        dataset size, including >1000-leaf stores.
        """
        membership = self._build_membership_index_base(leaves)  # stored_components = total
        return {
            "hash": composite_digest,
            "hashes": [{"algorithm": composite_algorithm, "digest": composite_digest}],
            "size": sum(leaf.size for leaf in leaves),
            "source_type": source_type,
            "session_hash": session_hash,
            "component_count_total": len(leaves),
            "components": [self._component_payload(leaf) for leaf in leaves],
            "membership_index": membership,
        }

    def cap_payload_for_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bound a composite payload for the GLaaS upload (wire size + paid storage).

        Returns a copy whose stored ``components`` are capped to ``_max_stored_components``
        and to ``_max_payload_bytes`` (binary search), with ``stored_components`` updated
        to match. The membership bloom — which covers ALL leaves — and
        ``component_count_total`` are left intact, so a query against the uploaded anchor
        still resolves every component. The full component list is kept locally; only this
        wire/GLaaS copy is bounded.
        """
        components = payload.get("components") or []
        membership = payload.get("membership_index") or {}
        max_candidate = min(len(components), self._max_stored_components)
        if max_candidate <= 0:
            return payload

        def with_count(stored_count: int) -> dict[str, Any]:
            capped = dict(payload)
            capped["components"] = components[:stored_count]
            capped["membership_index"] = {**membership, "stored_components": stored_count}
            return capped

        best = with_count(max_candidate)
        if self._payload_size(best) <= self._max_payload_bytes:
            return best

        low, high = 1, max_candidate
        best = with_count(1)
        while low <= high:
            mid = (low + high) // 2
            candidate = with_count(mid)
            if self._payload_size(candidate) <= self._max_payload_bytes:
                best, low = candidate, mid + 1
            else:
                high = mid - 1
        return best

    def _collect_leaves(
        self,
        root_path: Path,
        resolved_sources: list[ResolvedSource],
        hashes_by_path: dict[str, str],
    ) -> list[CompositeLeaf]:
        return self._drop_boilerplate(
            self._collect_raw_leaves(root_path, resolved_sources, hashes_by_path)
        )

    def _collect_raw_leaves(
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
                    component_algorithm="blake3",
                )
            )

        return leaves

    @staticmethod
    def _drop_boilerplate(leaves: list[CompositeLeaf]) -> list[CompositeLeaf]:
        """Exclude format/repo boilerplate (README, .gitattributes, …) from identity.

        Structure decides which files are identity-bearing; boilerplate is associated
        with the dataset but does not move its composite hash or membership.
        """
        if not leaves:
            return leaves
        detection = _detect([leaf.relative_path for leaf in leaves])
        boilerplate = set(detection.boilerplate)
        if not boilerplate:
            return leaves
        return [leaf for leaf in leaves if leaf.relative_path not in boilerplate]

    @staticmethod
    def _resolve_component_digest(path: Path, hashes_by_path: dict[str, str]) -> str | None:
        if path.is_symlink():
            return CompositeArtifactBuilder._symlink_target_digest(path)
        return hashes_by_path.get(str(path))

    @staticmethod
    def _resolve_component_size(path: Path, leaf_kind: str) -> int:
        if leaf_kind == "symlink":
            return len(CompositeArtifactBuilder._symlink_target_bytes(path))
        # register-time formation works from recorded (path, hash) metadata; the live
        # file may be gone. Size is not part of the composite identity, so fall back to 0.
        try:
            return path.stat().st_size
        except OSError:
            return 0

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

    def _compute_composite_digest(self, leaves: list[CompositeLeaf]) -> tuple[str, str]:
        """Path-sensitive composite digest + its algorithm name.

        Folds each component's relative path into the digest so moving a file (e.g.
        a validation shard into the train split) changes the composite identity.
        The *combiner* hash follows the source's content family — ``sha256`` when
        every component is sha256 (HF/asserted), else ``blake3`` (local, etag, …) —
        and the per-leaf component algorithm is preserved in the preimage. For a
        homogeneous sha256/blake3 component set this reproduces the ``sha256-tree`` /
        ``blake3-tree`` qualifying hash exactly (same domain and construction).
        """
        combiner = self._combiner_algorithm(leaves)
        triples = [(leaf.relative_path, leaf.component_algorithm, leaf.digest) for leaf in leaves]
        pre = _preimage(triples, domain=f"roar:{combiner}-tree:v1\n".encode())
        if combiner == "blake3":
            if _blake3 is None:
                raise RuntimeError("blake3 package is required for composite digest computation")
            digest = _blake3.blake3(pre).hexdigest()
        else:
            digest = hashlib.new(combiner, pre).hexdigest()
        return digest, f"composite-{combiner}"

    @staticmethod
    def _combiner_algorithm(leaves: list[CompositeLeaf]) -> str:
        """sha256 when all components are sha256 (HF), else blake3 (local default)."""
        return "sha256" if {leaf.component_algorithm for leaf in leaves} == {"sha256"} else "blake3"

    @staticmethod
    def _component_payload(leaf: CompositeLeaf) -> dict[str, Any]:
        return {
            "relative_path": leaf.relative_path,
            "leaf_kind": leaf.leaf_kind,
            "component_algorithm": leaf.component_algorithm,
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
            key = f"{leaf.component_algorithm}:{leaf.digest}".encode()
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
