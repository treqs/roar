"""
Unit tests for download backend base module.

Tests source URL parsing and the DownloadBackend ABC.
"""

import pytest

from roar.integrations.download.base import Source, parse_source


class TestParseSource:
    """Tests for source URL parsing."""

    def test_parse_s3_url(self):
        """Parse S3 URL extracts bucket and key."""
        result = parse_source("s3://my-bucket/path/to/model.pt")

        assert result.scheme == "s3"
        assert result.bucket == "my-bucket"
        assert result.key == "path/to/model.pt"
        assert result.original_url == "s3://my-bucket/path/to/model.pt"

    def test_parse_s3_prefix(self):
        """Parse S3 prefix URL (trailing slash stripped, original preserved)."""
        result = parse_source("s3://my-bucket/checkpoints/")

        assert result.scheme == "s3"
        assert result.bucket == "my-bucket"
        assert result.key == "checkpoints"
        assert result.original_url == "s3://my-bucket/checkpoints/"

    def test_parse_s3_bucket_only(self):
        """Parse S3 URL with just a bucket."""
        result = parse_source("s3://my-bucket")

        assert result.scheme == "s3"
        assert result.bucket == "my-bucket"
        assert result.key == ""

    def test_parse_gs_url(self):
        """Parse GCS URL extracts bucket and key."""
        result = parse_source("gs://my-bucket/models/v1.pt")

        assert result.scheme == "gs"
        assert result.bucket == "my-bucket"
        assert result.key == "models/v1.pt"

    def test_parse_gs_prefix(self):
        """Parse GCS prefix URL."""
        result = parse_source("gs://my-bucket/outputs/")

        assert result.scheme == "gs"
        assert result.bucket == "my-bucket"
        assert result.key == "outputs"

    def test_parse_http_url(self):
        """Parse HTTP URL."""
        result = parse_source("http://example.com/models/model.pt")

        assert result.scheme == "http"
        assert result.bucket == "example.com"
        assert result.key == "models/model.pt"
        assert result.original_url == "http://example.com/models/model.pt"

    def test_parse_https_url(self):
        """Parse HTTPS URL."""
        result = parse_source("https://huggingface.co/model/weights.bin")

        assert result.scheme == "https"
        assert result.bucket == "huggingface.co"
        assert result.key == "model/weights.bin"

    def test_parse_unsupported_scheme_raises(self):
        """Parsing unsupported scheme raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            parse_source("ftp://server/path")

        assert "Unsupported" in str(exc_info.value)
        assert "ftp" in str(exc_info.value)

    def test_parse_wandb_scheme_unsupported(self):
        """wandb:// is not a supported source scheme."""
        with pytest.raises(ValueError, match="Unsupported"):
            parse_source("wandb://project/artifact")


class TestSourceProperties:
    """Tests for Source dataclass properties."""

    def test_filename_from_key(self):
        """filename extracts the last component of the key."""
        source = Source(scheme="s3", bucket="b", key="path/to/model.pt", original_url="")
        assert source.filename == "model.pt"

    def test_filename_simple(self):
        """filename works for a simple key with no slashes."""
        source = Source(scheme="s3", bucket="b", key="model.pt", original_url="")
        assert source.filename == "model.pt"

    def test_filename_empty_for_prefix(self):
        """filename is empty for prefix-like keys."""
        source = Source(scheme="s3", bucket="b", key="", original_url="")
        assert source.filename == ""

    def test_is_prefix_trailing_slash(self):
        """is_prefix detects trailing slash via parse_source."""
        source = parse_source("s3://b/prefix/")
        assert source.is_prefix is True

    def test_is_prefix_empty_key(self):
        """is_prefix is True for bucket-root source."""
        source = parse_source("s3://b/")
        assert source.is_prefix is True

    def test_is_prefix_false_for_file(self):
        """is_prefix is False for file keys."""
        source = parse_source("s3://b/model.pt")
        assert source.is_prefix is False
