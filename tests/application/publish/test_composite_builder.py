"""Unit tests for composite artifact payload construction."""

import base64
import math
from pathlib import Path

import blake3

from roar.application.publish.composite_builder import CompositeArtifactBuilder, CompositeLeaf
from roar.application.publish.source_resolution import ResolvedSource


def _file_hash(path: Path) -> str:
    return blake3.blake3(path.read_bytes()).hexdigest()


def test_builder_is_deterministic_and_component_order_is_canonical(tmp_path: Path):
    root = tmp_path / "dataset"
    root.mkdir()
    a_file = root / "a.csv"
    b_file = root / "b.csv"
    a_file.write_text("a\n")
    b_file.write_text("b\n")

    a_source = ResolvedSource(path=a_file, exists=True, relative_key="a.csv", source_root=root)
    b_source = ResolvedSource(path=b_file, exists=True, relative_key="b.csv", source_root=root)
    hashes = {
        str(a_file): _file_hash(a_file),
        str(b_file): _file_hash(b_file),
    }

    builder = CompositeArtifactBuilder()
    first = builder.build_for_root(
        root_path=root,
        resolved_sources=[b_source, a_source],
        hashes_by_path=hashes,
        session_hash="session_123",
        source_type=None,
    )
    second = builder.build_for_root(
        root_path=root,
        resolved_sources=[a_source, b_source],
        hashes_by_path=hashes,
        session_hash="session_123",
        source_type=None,
    )

    assert first is not None
    assert second is not None
    assert first.digest == second.digest
    # path-sensitive blake3-tree digest (folds relative paths into the composite key)
    assert first.digest == "b7fece775c87fff407b46a93b341d3af99618f1f7a3d79d928fa789b46a77f1e"
    assert first.payload["hashes"] == [{"algorithm": "composite-blake3", "digest": first.digest}]
    assert [item["relative_path"] for item in first.payload["components"]] == ["a.csv", "b.csv"]
    membership = first.payload["membership_index"]
    assert membership["total_components"] == 2
    assert membership["stored_components"] == 2
    assert membership["bloom_bits"] == 2048
    assert membership["bloom_hashes"] == 12
    assert membership["bloom_version"] == 1
    bloom_filter = base64.b64decode(membership["bloom_filter_base64"])
    assert len(bloom_filter) == membership["bloom_bits"] // 8
    assert any(byte != 0 for byte in bloom_filter)


def test_builder_excludes_boilerplate_from_identity(tmp_path: Path):
    """README/.gitattributes are associated but do not move the composite hash."""
    root = tmp_path / "dataset"
    root.mkdir()
    shard = root / "shard_00000.parquet"
    shard.write_text("data\n")
    readme = root / "README.md"
    readme.write_text("# docs\n")

    shard_src = ResolvedSource(
        path=shard, exists=True, relative_key="shard_00000.parquet", source_root=root
    )
    readme_src = ResolvedSource(
        path=readme, exists=True, relative_key="README.md", source_root=root
    )
    hashes = {str(shard): _file_hash(shard), str(readme): _file_hash(readme)}
    builder = CompositeArtifactBuilder()

    with_readme = builder.build_for_root(
        root_path=root,
        resolved_sources=[shard_src, readme_src],
        hashes_by_path=hashes,
        session_hash="s",
        source_type=None,
    )
    without_readme = builder.build_for_root(
        root_path=root,
        resolved_sources=[shard_src],
        hashes_by_path=hashes,
        session_hash="s",
        source_type=None,
    )
    assert with_readme is not None and without_readme is not None
    assert with_readme.digest == without_readme.digest  # README excluded from identity
    paths = [c["relative_path"] for c in with_readme.payload["components"]]
    assert "README.md" not in paths


def test_builder_digest_is_path_sensitive(tmp_path: Path):
    """Same content at a different path -> different composite key (principle 4)."""
    root = tmp_path / "ds"
    root.mkdir()
    f = root / "train.parquet"
    f.write_text("x\n")
    digest = _file_hash(f)

    def build(rel: str) -> str:
        result = CompositeArtifactBuilder().build_for_leaves(
            root_path=str(root),
            leaves=[
                CompositeLeaf(relative_path=rel, digest=digest, size=2, component_type=None),
                CompositeLeaf(
                    relative_path="other.parquet", digest="aa" * 16, size=2, component_type=None
                ),
            ],
            session_hash="s",
            source_type=None,
        )
        assert result is not None
        return result.digest

    assert build("train/0.parquet") != build("validation/0.parquet")


def test_builder_hashes_symlink_leaf_from_link_target_bytes(tmp_path: Path):
    root = tmp_path / "dataset"
    root.mkdir()
    target_file = root / "target.txt"
    target_file.write_text("target content\n")
    link_file = root / "alias.txt"
    link_file.symlink_to("target.txt")

    sources = [
        ResolvedSource(path=target_file, exists=True, relative_key="target.txt", source_root=root),
        ResolvedSource(path=link_file, exists=True, relative_key="alias.txt", source_root=root),
    ]
    hashes = {
        str(target_file): _file_hash(target_file),
        # Batch hashing in put follows symlinks; this intentionally mirrors that input.
        str(link_file): _file_hash(target_file),
    }

    builder = CompositeArtifactBuilder()
    result = builder.build_for_root(
        root_path=root,
        resolved_sources=sources,
        hashes_by_path=hashes,
        session_hash="session_123",
        source_type=None,
    )

    assert result is not None
    components = {item["relative_path"]: item for item in result.payload["components"]}
    alias_component = components["alias.txt"]
    target_component = components["target.txt"]
    expected_symlink_digest = blake3.blake3(b"target.txt").hexdigest()

    assert alias_component["leaf_kind"] == "symlink"
    assert alias_component["component_digest"] == expected_symlink_digest
    assert alias_component["component_digest"] != target_component["component_digest"]
    assert alias_component["component_type"] == "inode/symlink"
    assert alias_component["component_size"] == len(b"target.txt")


def test_builder_caps_stored_components_and_emits_membership_index(tmp_path: Path):
    root = tmp_path / "dataset"
    root.mkdir()

    sources: list[ResolvedSource] = []
    hashes: dict[str, str] = {}
    for index in range(5):
        file_path = root / f"leaf_{index}.parquet"
        file_path.write_text(f"leaf-{index}\n")
        sources.append(
            ResolvedSource(
                path=file_path,
                exists=True,
                relative_key=file_path.name,
                source_root=root,
            )
        )
        hashes[str(file_path)] = _file_hash(file_path)

    builder = CompositeArtifactBuilder(max_stored_components=3)
    result = builder.build_for_root(
        root_path=root,
        resolved_sources=sources,
        hashes_by_path=hashes,
        session_hash="session_123",
        source_type="s3",
    )

    assert result is not None
    # The built (local) payload keeps EVERY component — resolution must be exact.
    assert result.component_count_total == 5
    assert result.component_count_stored == 5
    assert len(result.payload["components"]) == 5
    membership = result.payload["membership_index"]
    assert membership["total_components"] == 5
    assert membership["stored_components"] == 5
    assert membership["bloom_bits"] == 2048
    assert membership["bloom_hashes"] == 12
    assert membership["bloom_version"] == 1

    bloom_filter = base64.b64decode(membership["bloom_filter_base64"])
    assert len(bloom_filter) == membership["bloom_bits"] // 8
    assert any(byte != 0 for byte in bloom_filter)

    # The UPLOAD copy is capped to max_stored_components; the bloom (over all 5) and the
    # total are preserved so the uploaded anchor still resolves every component.
    capped = builder.cap_payload_for_upload(result.payload)
    assert len(capped["components"]) == 3
    assert capped["component_count_total"] == 5
    assert capped["membership_index"]["stored_components"] == 3
    assert capped["membership_index"]["bloom_filter_base64"] == membership["bloom_filter_base64"]


def test_builder_payload_size_cap_reduces_stored_components(tmp_path: Path):
    root = tmp_path / "dataset"
    root.mkdir()

    sources: list[ResolvedSource] = []
    hashes: dict[str, str] = {}
    for index in range(120):
        file_path = root / f"leaf_{index:04d}.jsonl"
        file_path.write_text(f'{{"id": {index}, "value": {index * 3}}}\n')
        sources.append(
            ResolvedSource(
                path=file_path,
                exists=True,
                relative_key=file_path.name,
                source_root=root,
            )
        )
        hashes[str(file_path)] = _file_hash(file_path)

    # Intentionally tiny payload cap to force stored component reduction on UPLOAD.
    builder = CompositeArtifactBuilder(max_stored_components=1000, max_payload_bytes=4096)
    result = builder.build_for_root(
        root_path=root,
        resolved_sources=sources,
        hashes_by_path=hashes,
        session_hash="session_123",
        source_type="s3",
    )

    assert result is not None
    # Local build keeps all 120 components (no payload-size cap locally).
    assert result.component_count_total == 120
    assert result.component_count_stored == 120
    assert len(result.payload["components"]) == 120

    # The upload copy is shrunk to fit the payload-byte budget; total + bloom intact.
    capped = builder.cap_payload_for_upload(result.payload)
    assert len(capped["components"]) < 120
    assert builder._payload_size(capped) <= 4096
    assert capped["component_count_total"] == 120
    membership = capped["membership_index"]
    assert membership["stored_components"] == len(capped["components"])
    assert (
        membership["bloom_filter_base64"]
        == result.payload["membership_index"]["bloom_filter_base64"]
    )


def test_bloom_parameters_target_point_one_percent_false_positive_rate():
    total_components = 1105

    bloom_bits = CompositeArtifactBuilder._choose_bloom_bits(total_components)
    bloom_hashes = CompositeArtifactBuilder._choose_bloom_hashes(total_components, bloom_bits)

    estimated_false_positive_rate = (
        1.0 - math.exp(-(bloom_hashes * total_components) / bloom_bits)
    ) ** bloom_hashes

    assert bloom_hashes == 10
    assert estimated_false_positive_rate <= 0.001


def test_builder_supports_prehashed_leaves_with_non_blake_component_algorithms():
    builder = CompositeArtifactBuilder()

    result = builder.build_for_leaves(
        root_path="s3://test-bucket/sensor_data",
        leaves=[
            CompositeLeaf(
                relative_path="shard_000000.parquet",
                digest="11" * 16,
                size=128,
                component_type="application/vnd.apache.parquet",
                component_algorithm="etag",
            ),
            CompositeLeaf(
                relative_path="shard_000001.parquet",
                digest="22" * 16,
                size=256,
                component_type="application/vnd.apache.parquet",
                component_algorithm="etag",
            ),
        ],
        session_hash="",
        source_type="s3",
    )

    assert result is not None
    assert result.root_path == "s3://test-bucket/sensor_data"
    assert result.payload["hashes"][0]["algorithm"] == "composite-blake3"
    assert [item["component_algorithm"] for item in result.payload["components"]] == [
        "etag",
        "etag",
    ]
