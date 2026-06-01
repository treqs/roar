"""HF download backend — URL parsing (offline) + live manifest/download (gated)."""

from __future__ import annotations

import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from roar.integrations.download.base import parse_source
from roar.integrations.download.hf import HFDownloadBackend, parse_hf_url


def _online() -> bool:
    try:
        urllib.request.urlopen("https://huggingface.co/api/whoami-v2", timeout=5)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


# --- offline: URL parsing --------------------------------------------------
def test_parse_dataset_url():
    assert parse_hf_url("hf://datasets/karpathy/climbmix-400b-shuffle") == (
        "datasets",
        "karpathy/climbmix-400b-shuffle",
        "main",
        "",
    )


def test_parse_pinned_commit_and_subpath():
    assert parse_hf_url("hf://datasets/nvidia/esm2@abc123") == (
        "datasets",
        "nvidia/esm2",
        "abc123",
        "",
    )
    assert parse_hf_url("hf://datasets/physical-intelligence/libero/meta") == (
        "datasets",
        "physical-intelligence/libero",
        "main",
        "meta",
    )


def test_parse_repo_type_default_and_models():
    # bare owner/name defaults to a dataset repo
    assert parse_hf_url("hf://bert-base/uncased") == ("datasets", "bert-base/uncased", "main", "")
    # explicit models/ prefix selects the model repo type
    assert parse_hf_url("hf://models/openai/clip@v1") == ("models", "openai/clip", "v1", "")


def test_parse_source_routes_hf_scheme():
    src = parse_source("hf://datasets/physical-intelligence/libero/meta")
    assert src.scheme == "hf"
    assert src.is_prefix is True  # a directory subpath
    single = parse_source("hf://datasets/physical-intelligence/libero/meta/info.json")
    assert single.is_prefix is False  # a concrete file


def test_parse_invalid():
    with pytest.raises(ValueError):
        parse_hf_url("s3://bucket/key")
    with pytest.raises(ValueError):
        parse_hf_url("hf://owneronly")


# --- live (network-gated): manifest, list, download, verify ----------------
pytestmark_live = pytest.mark.skipif(not _online(), reason="no network to huggingface.co")

LIBERO = "hf://datasets/physical-intelligence/libero/meta"


@pytest.mark.skipif(not _online(), reason="no network")
def test_live_commit_and_manifest_have_sha256():
    b = HFDownloadBackend(parse_source(LIBERO))
    assert len(b.commit) == 40
    files = {f.path: f for f in b.manifest()}
    assert "meta/info.json" in files
    # meta files are non-LFS git blobs (no sha256); episodes parquet are LFS
    assert b.coordinates["host"] == "hf"
    assert b.coordinates["repo"] == "physical-intelligence/libero"


@pytest.mark.skipif(not _online(), reason="no network")
def test_live_sha256_of_nonlfs_meta_changes_composite_identity():
    """Identity-bearing non-LFS meta/ files are re-hashed into the composite, so the
    LFS+meta key differs from the LFS-only key (meta is part of identity)."""
    import mimetypes

    from roar.application.composite import detect
    from roar.application.publish.composite_builder import CompositeArtifactBuilder, CompositeLeaf

    b = HFDownloadBackend(
        parse_source(
            "hf://datasets/physical-intelligence/libero"
            "@a4336d589d589045d1c56423ffdf3b88a0e19b1f"
        )
    )
    m = b.manifest()
    boiler = set(detect([f.path for f in m]).boilerplate)
    identity = [f for f in m if f.path not in boiler]

    def leaf(f, digest):
        return CompositeLeaf(
            relative_path=f.path,
            digest=digest,
            size=f.size,
            component_type=mimetypes.guess_type(f.path)[0],
            component_algorithm="sha256",
        )

    lfs_only = [leaf(f, f.sha256) for f in identity if f.is_lfs and f.sha256]
    with_meta = lfs_only + [leaf(f, b.sha256_of(f.path)) for f in identity if not f.is_lfs]
    builder = CompositeArtifactBuilder()
    d_lfs = builder.build_for_leaves(root_path="x", leaves=lfs_only, session_hash="", source_type="hf")
    d_all = builder.build_for_leaves(root_path="x", leaves=with_meta, session_hash="", source_type="hf")
    assert d_lfs.digest == "9f01856f31ff9ee83e5c43aa494fb63d7517fc9d028422c9701d8aeadc1f86bb"
    assert d_all.digest != d_lfs.digest  # meta/ files fold into identity


@pytest.mark.skipif(not _online(), reason="no network")
def test_live_list_keys_scoped_to_subpath():
    b = HFDownloadBackend(parse_source(LIBERO))
    keys = b.list_keys("")
    assert keys == sorted(keys)
    assert all(k.startswith("meta/") for k in keys)
    assert "meta/info.json" in keys


@pytest.mark.skipif(not _online(), reason="no network")
def test_live_manifest_composite_sha256_matches_anchor():
    """The sha256-tree over an HF dataset's LFS oids is the composite-sha256 key that
    `roar get` forms — pinned against the esm2 anchor."""
    import mimetypes

    from roar.application.composite import detect
    from roar.application.publish.composite_builder import CompositeArtifactBuilder, CompositeLeaf

    b = HFDownloadBackend(
        parse_source(
            "hf://datasets/nvidia/esm2_uniref_pretraining_data"
            "@4ac1d2973567e46b8ca95901f4b4793a21305995"
        )
    )
    manifest = b.manifest()
    boiler = set(detect([f.path for f in manifest]).boilerplate)
    leaves = [
        CompositeLeaf(
            relative_path=f.path,
            digest=f.sha256,
            size=f.size,
            component_type=mimetypes.guess_type(f.path)[0],
            component_algorithm="sha256",
        )
        for f in manifest
        if f.is_lfs and f.sha256 and f.path not in boiler
    ]
    result = CompositeArtifactBuilder().build_for_leaves(
        root_path="hf://x", leaves=leaves, session_hash="", source_type="hf"
    )
    assert result is not None
    assert result.payload["hashes"][0]["algorithm"] == "composite-sha256"
    assert result.digest == "d70b5ec319e39091281ff5970bfd7647cf38eb9cfa6de0cfdd074207264ce4b4"


@pytest.mark.skipif(not _online(), reason="no network")
def test_live_download_and_exists():
    b = HFDownloadBackend(parse_source(LIBERO))
    d = Path(tempfile.mkdtemp())
    b.download("meta/info.json", d / "info.json")
    assert (d / "info.json").stat().st_size > 0
    assert b.exists("meta/info.json") is True
    assert b.exists("meta/does-not-exist.json") is False


@pytest.mark.skipif(not _online(), reason="no network")
@pytest.mark.skipif(
    not Path("~/.hf_token").expanduser().exists() and not os.environ.get("HF_TOKEN"),
    reason="no HF token",
)
def test_live_lfs_download_verifies_sha256():
    """Downloading an LFS file verifies its bytes against the published sha256."""
    b = HFDownloadBackend(parse_source("hf://datasets/physical-intelligence/libero"))
    lfs = next((f for f in b.manifest() if f.is_lfs), None)
    if lfs is None:
        pytest.skip("no LFS file found")
    d = Path(tempfile.mkdtemp())
    # download verifies internally; a mismatch would raise
    b.download(lfs.path, d / "f.bin")
    import hashlib

    assert hashlib.sha256((d / "f.bin").read_bytes()).hexdigest() == lfs.sha256
