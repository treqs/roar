"""Bug 3 regression: the lineage/register path must treat composite-sha256 (HF get)
as a composite, not silently drop it as a plain artifact."""

from __future__ import annotations

from roar.application.publish.lineage_composites import (
    has_lineage_composites,
    resolve_component_hash_for_registration,
)
from roar.application.publish.registration import (
    extract_composite_digest,
    normalize_registration_source_type,
)
from roar.core.digests import COMPOSITE_ALGORITHMS, is_composite_algorithm


def test_is_composite_algorithm_covers_both_families():
    assert {"composite-blake3", "composite-sha256"} == COMPOSITE_ALGORITHMS
    assert is_composite_algorithm("composite-blake3")
    assert is_composite_algorithm("composite-sha256")
    assert not is_composite_algorithm("blake3")
    assert not is_composite_algorithm("sha256")
    assert not is_composite_algorithm(None)


def test_extract_composite_digest_recognizes_sha256():
    assert extract_composite_digest([{"algorithm": "composite-sha256", "digest": "a" * 64}]) == (
        "a" * 64
    )
    assert extract_composite_digest([{"algorithm": "composite-blake3", "digest": "b" * 64}]) == (
        "b" * 64
    )
    assert extract_composite_digest([{"algorithm": "blake3", "digest": "c" * 64}]) is None


def test_has_lineage_composites_for_sha256_artifact():
    artifact = {"hashes": [{"algorithm": "composite-sha256", "digest": "a" * 64}]}
    assert has_lineage_composites([artifact]) is True


def test_resolve_component_returns_sha256_not_dropped():
    """An HF (sha256) component with no linked blake3 artifact resolves to sha256,
    not None (which previously dropped it -> storedComponents=0)."""
    row = {
        "relative_path": "train/000.parquet",
        "component_algorithm": "sha256",
        "component_digest": "D" * 64,
    }
    resolved = resolve_component_hash_for_registration(
        row=row, db_ctx=object(), lineage_artifacts_by_id={}, logger=_NullLogger()
    )
    assert resolved == ("sha256", "d" * 64)


def test_resolve_component_still_handles_blake3():
    row = {"relative_path": "x", "component_algorithm": "blake3", "component_digest": "E" * 64}
    resolved = resolve_component_hash_for_registration(
        row=row, db_ctx=object(), lineage_artifacts_by_id={}, logger=_NullLogger()
    )
    assert resolved == ("blake3", "e" * 64)


def test_hf_source_type_is_accepted():
    assert normalize_registration_source_type("hf") == "hf"
    assert normalize_registration_source_type("s3") == "s3"


class _NullLogger:
    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass
