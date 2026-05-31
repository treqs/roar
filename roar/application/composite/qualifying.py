"""The qualifying composite hash: ``sha256-tree``.

A :class:`Leaf` is one file's content identity within a composite. ``kind``
records whether the file is dataset ``data``, format-declared ``meta`` (e.g.
LeRobot ``meta/``), or excluded ``boilerplate`` (``.gitattributes``/``README``);
only ``data`` and ``meta`` leaves enter identity.

``verified`` distinguishes *asserted* identity (a sha256 taken from a host's
published metadata, e.g. an HF LFS oid — trusts the host) from *verified* identity
(a sha256 roar computed from the bytes). Verification never changes the digest —
same bytes, same sha256 — it only annotates trust.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .canonical import NESTED_ALGO, QUALIFYING_ALGO, preimage

_IDENTITY_KINDS = ("data", "meta")


@dataclass(frozen=True)
class Leaf:
    relpath: str
    sha256: str
    kind: str = "data"  # data | meta | boilerplate
    size: int | None = None
    verified: bool = False  # False => asserted (from host metadata)


@dataclass(frozen=True)
class Composite:
    digest: str  # the sha256-tree hex
    algo: str
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


def sha256_tree(leaves: list[Leaf]) -> str:
    """Qualifying composite hash over identity-bearing leaves (data + meta)."""
    triples = [
        (leaf.relpath, QUALIFYING_ALGO, leaf.sha256)
        for leaf in leaves
        if leaf.kind in _IDENTITY_KINDS
    ]
    return hashlib.sha256(preimage(triples)).hexdigest()


def build(leaves: list[Leaf], *, kind: str | None = None) -> Composite:
    """Build a flat composite from leaves."""
    return Composite(
        digest=sha256_tree(leaves),
        algo=NESTED_ALGO,
        leaves=tuple(leaves),
        kind=kind,
    )


def build_nested(children: list[tuple[str, Composite]], *, kind: str | None = None) -> Composite:
    """Composite-of-composites: a parent whose leaves are child composite hashes.

    Used when a declared dataset contains independently-structured sub-datasets
    (e.g. a directory of ``.zarr`` stores). The parent's identity is over
    ``(child_relpath, child.digest)`` tagged with the ``sha256-tree`` algorithm, so
    nesting is preserved and the result is still purely content-derived.
    """
    triples = [(relpath, NESTED_ALGO, child.digest) for relpath, child in children]
    digest = hashlib.sha256(preimage(triples)).hexdigest()
    return Composite(
        digest=digest,
        algo=NESTED_ALGO,
        leaves=(),
        nested=tuple(child for _relpath, child in children),
        kind=kind,
    )
