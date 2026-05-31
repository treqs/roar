"""Canonical preimage for the qualifying composite hash (``sha256-tree``).

The qualifying hash is roar's host-agnostic, content-addressable dataset key: a
``sha256`` over a canonical, injective serialization of a composite's leaves. It is
path-sensitive (moving a file changes the key), content-only (no host metadata
enters), and locally computable, so the key registered by ``roar put`` equals the
key produced by ``roar get`` for the same bytes and layout.

This is distinct from roar's internal ``blake3`` file hashing: blake3 stays the
fast hash for primitive file lineage; ``sha256-tree`` is the *dataset* key.

Frozen decisions (any change is a version bump of the domain tag):
  - leaf = (normalized_posix_relpath, "<component-algo>:<hexdigest>")
  - component-algo is always ``sha256`` for a qualifying leaf; nested composites
    use ``sha256-tree``
  - leaves are sorted by the UTF-8 bytes of their normalized relpath
  - size and mode do not enter the preimage
  - NUL separates path from digest; newline terminates each leaf line
  - a domain-separation prefix prevents cross-protocol collisions
"""

from __future__ import annotations

DOMAIN_V1 = b"roar:sha256-tree:v1\n"
QUALIFYING_ALGO = "sha256"
NESTED_ALGO = "sha256-tree"


def normalize_relpath(path: str) -> str:
    """Normalize a relative path to canonical POSIX form.

    Backslashes become forward slashes, a leading ``./`` and leading slashes are
    stripped. ``..`` components are rejected (a leaf must stay within its composite
    root) and an empty result is an error.
    """
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if not p:
        raise ValueError("empty relative path is not a valid leaf key")
    if any(segment == ".." for segment in p.split("/")):
        raise ValueError(f"path escapes composite root: {path!r}")
    return p


def leaf_line(relpath: str, algo: str, hexdigest: str) -> bytes:
    """Serialize one leaf to its canonical line (relpath already normalized)."""
    if not hexdigest:
        raise ValueError(f"leaf {relpath!r} has empty digest")
    return relpath.encode("utf-8") + b"\x00" + f"{algo}:{hexdigest.lower()}".encode() + b"\n"


def preimage(leaves: list[tuple[str, str, str]], *, domain: bytes = DOMAIN_V1) -> bytes:
    """Build the canonical preimage from ``(relpath, algo, hexdigest)`` triples.

    Relpaths are normalized and the set is sorted by normalized relpath bytes.
    Duplicate normalized relpaths are rejected.
    """
    normalized: dict[str, tuple[str, str]] = {}
    for relpath, algo, hexdigest in leaves:
        key = normalize_relpath(relpath)
        if key in normalized:
            raise ValueError(f"duplicate leaf path in composite: {key!r}")
        normalized[key] = (algo, hexdigest)

    out = bytearray(domain)
    for key in sorted(normalized, key=lambda k: k.encode("utf-8")):
        algo, hexdigest = normalized[key]
        out += leaf_line(key, algo, hexdigest)
    return bytes(out)
