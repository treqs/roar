"""The qualifying composite hash: ``<algo>-tree``.

The composite key follows the *source's natural content hash*, not a forced single
algorithm: a dataset built locally is keyed ``blake3-tree`` (roar already hashed it
with blake3 during the run — no re-hashing), while a dataset fetched from HF is
keyed ``sha256-tree`` (HF publishes sha256 LFS oids for free). The component
digests and the membership bloom use the same algorithm, so identity is internally
consistent. The ``-tree`` *construction* is one standard, path-sensitive canonical
form; only the content hash flowing through it varies by provenance.

A :class:`Leaf` is one file's content identity. ``kind`` records dataset ``data``,
format-declared ``meta`` (e.g. LeRobot ``meta/``), or excluded ``boilerplate``;
only ``data`` and ``meta`` leaves enter identity. ``verified`` distinguishes
*asserted* identity (a digest taken from a host's published metadata — trusts the
host) from *verified* identity (a digest roar computed from the bytes). Verification
never changes the digest; it only annotates trust.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .canonical import preimage

_IDENTITY_KINDS = ("data", "meta")
DEFAULT_ALGO = "sha256"


def _hasher(algo: str) -> Any:
    """Return a fresh hasher for the content algorithm (blake3 or a hashlib name)."""
    if algo == "blake3":
        import blake3

        return blake3.blake3()
    return hashlib.new(algo)


def _domain(algo: str) -> bytes:
    """Per-algorithm domain-separation prefix (``roar:<algo>-tree:v1``)."""
    return f"roar:{algo}-tree:v1\n".encode()


@dataclass(frozen=True)
class Leaf:
    relpath: str
    digest: str
    kind: str = "data"  # data | meta | boilerplate
    size: int | None = None
    verified: bool = False  # False => asserted (from host metadata)


@dataclass(frozen=True)
class Composite:
    digest: str  # the <algo>-tree hex
    algo: str  # e.g. "sha256-tree" | "blake3-tree"
    leaves: tuple[Leaf, ...]
    nested: tuple[Composite, ...] = field(default_factory=tuple)
    kind: str | None = None  # structural format name; a LABEL, not part of the key

    @property
    def verified(self) -> bool:
        """True iff every identity-bearing leaf was content-verified."""
        return all(leaf.verified for leaf in self.leaves if leaf.kind in _IDENTITY_KINDS)

    @property
    def identity_leaves(self) -> tuple[Leaf, ...]:
        return tuple(leaf for leaf in self.leaves if leaf.kind in _IDENTITY_KINDS)


def tree(leaves: list[Leaf], *, algo: str = DEFAULT_ALGO) -> str:
    """Qualifying composite hash over identity-bearing leaves, in ``<algo>-tree``.

    ``algo`` is the per-file content algorithm (``sha256`` for HF/asserted,
    ``blake3`` for local) and is used both for the leaf digest label and the outer
    combiner, so the key, components, and bloom all agree.
    """
    triples = [(leaf.relpath, algo, leaf.digest) for leaf in leaves if leaf.kind in _IDENTITY_KINDS]
    hasher = _hasher(algo)
    hasher.update(preimage(triples, domain=_domain(algo)))
    return hasher.hexdigest()


def sha256_tree(leaves: list[Leaf]) -> str:
    """``tree`` specialized to sha256 (HF / asserted identity)."""
    return tree(leaves, algo="sha256")


def blake3_tree(leaves: list[Leaf]) -> str:
    """``tree`` specialized to blake3 (locally-produced identity)."""
    return tree(leaves, algo="blake3")


def build(leaves: list[Leaf], *, algo: str = DEFAULT_ALGO, kind: str | None = None) -> Composite:
    """Build a flat composite from leaves keyed by the source's content algorithm."""
    return Composite(
        digest=tree(leaves, algo=algo),
        algo=f"{algo}-tree",
        leaves=tuple(leaves),
        kind=kind,
    )


def build_nested(
    children: list[tuple[str, Composite]],
    *,
    algo: str = DEFAULT_ALGO,
    kind: str | None = None,
) -> Composite:
    """Composite-of-composites: a parent whose leaves are child composite hashes.

    Used when a declared dataset contains independently-structured sub-datasets
    (e.g. a directory of ``.zarr`` stores). The parent's identity is over
    ``(child_relpath, child.digest)`` tagged ``<algo>-tree``, so nesting is preserved
    and the result is still purely content-derived.
    """
    nested_label = f"{algo}-tree"
    triples = [(relpath, nested_label, child.digest) for relpath, child in children]
    hasher = _hasher(algo)
    hasher.update(preimage(triples, domain=_domain(algo)))
    return Composite(
        digest=hasher.hexdigest(),
        algo=nested_label,
        leaves=(),
        nested=tuple(child for _relpath, child in children),
        kind=kind,
    )
