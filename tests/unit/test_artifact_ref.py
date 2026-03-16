"""Tests for roar.integrations.glaas.registration._artifact_ref shared helpers."""

from roar.integrations.glaas.registration._artifact_ref import (
    cache_key,
    extract_digest,
    preview,
)


class TestCacheKey:
    def test_single_hash(self):
        assert cache_key({"hash": "ABCD1234"}) == "hash:abcd1234"

    def test_hashes_list(self):
        item = {"hashes": [{"algorithm": "blake3", "digest": "ABCD1234"}]}
        assert cache_key(item) == "blake3:abcd1234"

    def test_empty_returns_none(self):
        assert cache_key({}) is None

    def test_empty_hash_string_returns_none(self):
        assert cache_key({"hash": ""}) is None

    def test_non_string_hash_returns_none(self):
        assert cache_key({"hash": 123}) is None

    def test_hashes_list_skips_non_dicts(self):
        item = {"hashes": ["not-a-dict", {"algorithm": "sha256", "digest": "ABC"}]}
        assert cache_key(item) == "sha256:abc"


class TestPreview:
    def test_artifact_hash_preferred(self):
        assert preview({"artifact_hash": "hash-123456789012"}) == "hash-1234567"

    def test_artifact_id_fallback(self):
        assert preview({"artifact_id": "id-123456789012"}) == "id-123456789"

    def test_single_hash(self):
        assert preview({"hash": "abcdef123456789"}) == "abcdef123456"

    def test_hashes_list(self):
        item = {"hashes": [{"digest": "abcdef123456789"}]}
        assert preview(item) == "abcdef123456"

    def test_fallback_to_path(self):
        assert preview({"path": "/some/path"}) == "/some/path"

    def test_empty_returns_none(self):
        assert preview({}) is None


class TestExtractDigest:
    def test_single_hash(self):
        assert extract_digest({"hash": "ABCD1234"}) == "abcd1234"

    def test_prefers_blake3(self):
        item = {
            "hashes": [
                {"algorithm": "sha256", "digest": "SHA_DIGEST"},
                {"algorithm": "blake3", "digest": "BLAKE_DIGEST"},
            ]
        }
        assert extract_digest(item) == "blake_digest"

    def test_fallback_to_first(self):
        item = {"hashes": [{"algorithm": "sha256", "digest": "SHA_DIGEST"}]}
        assert extract_digest(item) == "sha_digest"

    def test_empty_returns_none(self):
        assert extract_digest({}) is None
