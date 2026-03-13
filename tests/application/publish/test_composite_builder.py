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
    assert first.digest == "8270918dbe04aab28a3b245ca1e44e5a3884f70061de2966944b6b8985bc3da8"
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
    assert result.component_count_total == 5
    assert result.component_count_stored == 3
    assert len(result.payload["components"]) == 3
    assert result.payload["component_count_total"] == 5
    membership = result.payload["membership_index"]
    assert membership["total_components"] == 5
    assert membership["stored_components"] == 3
    assert membership["bloom_bits"] == 2048
    assert membership["bloom_hashes"] == 12
    assert membership["bloom_version"] == 1

    bloom_filter = base64.b64decode(membership["bloom_filter_base64"])
    assert len(bloom_filter) == membership["bloom_bits"] // 8
    assert any(byte != 0 for byte in bloom_filter)


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

    # Intentionally tiny payload cap to force stored component reduction.
    builder = CompositeArtifactBuilder(max_stored_components=1000, max_payload_bytes=4096)
    result = builder.build_for_root(
        root_path=root,
        resolved_sources=sources,
        hashes_by_path=hashes,
        session_hash="session_123",
        source_type="s3",
    )

    assert result is not None
    assert result.component_count_total == 120
    assert result.component_count_stored < 120
    membership = result.payload["membership_index"]
    assert membership["stored_components"] == result.component_count_stored


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
