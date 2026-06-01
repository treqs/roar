"""The `roar show <artifact>` reproduce hint must echo the content hash.

Regression for a 0.3.4 friction report: the hint printed the internal DB id,
which differed from the content hash shown in the header — reading as a
second, ambiguous hash. `roar reproduce` resolves by artifact hash, so the
hint should use the primary content digest (blake3 if present).
"""

from __future__ import annotations

from types import SimpleNamespace

from roar.application.query.results import ShowHashSummary
from roar.cli.commands.show import _primary_artifact_hash


def _summary(hashes):
    # _primary_artifact_hash only reads `.hashes`; a light stub keeps the test
    # focused without constructing the full ShowArtifactSummary dataclass.
    return SimpleNamespace(hashes=hashes)


def test_prefers_blake3_digest() -> None:
    summary = _summary(
        [ShowHashSummary("sha256", "sha-digest"), ShowHashSummary("blake3", "b3-digest")]
    )
    assert _primary_artifact_hash(summary) == "b3-digest"


def test_falls_back_to_first_hash_when_no_blake3() -> None:
    summary = _summary([ShowHashSummary("sha256", "sha-digest")])
    assert _primary_artifact_hash(summary) == "sha-digest"


def test_returns_none_without_hashes() -> None:
    assert _primary_artifact_hash(_summary([])) is None
    assert _primary_artifact_hash(_summary(None)) is None
