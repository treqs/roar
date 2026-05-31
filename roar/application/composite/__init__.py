"""Composite artifacts v2 — qualifying hash + structural detection.

The host-agnostic ``sha256-tree`` qualifying hash (the dataset GLaaS key) and the
declared/structural detectors that decide what a composite is and which files carry
its identity. Composites form at the boundaries (``get``/``put``/``register``), not
inside ``roar run``.

This package is additive; it does not yet replace the legacy heuristic composite
path in ``roar.application.publish``.
"""

from .canonical import normalize_relpath, preimage
from .detect import Detection, detect, detect_nested
from .qualifying import Composite, Leaf, build, build_nested, sha256_tree

__all__ = [
    "Composite",
    "Detection",
    "Leaf",
    "build",
    "build_nested",
    "detect",
    "detect_nested",
    "normalize_relpath",
    "preimage",
    "sha256_tree",
]
